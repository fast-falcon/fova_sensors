#!/usr/bin/env python3
import subprocess
import os
import signal
import threading
import wave
import audioop  # هشدار deprecate در 3.13 فعلاً مهم نیست

# ---------- تنظیمات کارت و فایل ----------
CARD = "0"
DEVICE = "0"

# فایل خام (native) که tinycap می‌نویسد
RAW_FILENAME = "usb2_raw.wav"

# فایل نهایی کم‌حجم که نگه می‌داریم
FINAL_FILENAME = "usb2.wav"

# تنظیمات واقعی کارت داخلی (native)
NATIVE_RATE = 48000       # روی این ریت صدا طبیعی بود
NATIVE_CHANNELS = 2
SAMPLE_WIDTH = 2          # 16-bit

# تنظیمات نسخه کم‌حجم خروجی
TARGET_RATE = 16000
TARGET_CHANNELS = 1       # mono

# تنظیمات فیلتر و گیت سکوت
HIGHPASS_CUTOFF_HZ = 100.0    # کات‌آف های‌پس (فرکانس‌های زیر این، ضعیف می‌شوند)
SILENCE_RMS_THRESHOLD = 200   # فقط پنجره‌هایی که RMS < این مقدار باشند صفر می‌شوند
SILENCE_WINDOW_MS = 10        # طول پنجره برای تشخیص سکوت (میلی‌ثانیه)

CMD = [
    "su_env",
    "tinycap", RAW_FILENAME,
    "-D", CARD,
    "-d", DEVICE,
    "-r", str(NATIVE_RATE),
    "-b", "16",
    "-c", str(NATIVE_CHANNELS),
]


# ---------- تابع تنظیم میکسر قبل از ضبط ----------
def configure_mixer():
    """
    تنظیم خودکار میکسر کارت 0:
      - Capture Volume = 63
      - Capture Switch = On
      - ADC PCM Capture Volume = 2
    """

    def run_mix(args):
        cmd = ["su_env", "tinymix", "-D", CARD] + args
        print("[mix]", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            print("[mix] ERROR:", e)

    # 0: Capture Volume  ->  63 (برای هر دو کانال)
    run_mix(["0", "63"])

    # 2: Capture Switch  ->  On
    run_mix(["2", "1"])

    # 29: ADC PCM Capture Volume -> 2
    run_mix(["29", "2"])

    print("[*] Mixer configured for capture on card 0.")


# ---------- نمایش لاگ tinycap روی ترمینال ----------
def stream_output(proc):
    if proc.stdout is None:
        return
    for line in proc.stdout:
        print(line, end="")


# ---------- درست کردن هدر WAV بعد از kill ----------
def fix_wav_header(path, channels, rate, sample_width):
    if not os.path.exists(path):
        print(f"[!] File {path} does not exist, skipping header fix.")
        return

    with open(path, "rb") as f:
        data = f.read()

    if len(data) <= 44:
        print(f"[!] File {path} is too small ({len(data)} bytes), skipping header fix.")
        return

    # tinycap اول یه هدر ناقص می‌نویسه، ما ۴۴ بایت اول را دور می‌ریزیم
    raw_pcm = data[44:]

    tmp_path = path + ".fixed"

    print(f"[*] Fixing WAV header for {path} (len(raw)={len(raw_pcm)} bytes)...")
    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(rate)
        w.writeframes(raw_pcm)

    os.replace(tmp_path, path)
    print(f"[*] Header fixed and written back to {path}.")


# ---------- فیلتر های‌پس ساده (یک‌قطبی) ----------
def highpass_filter(pcm, sampwidth, rate, cutoff_hz=HIGHPASS_CUTOFF_HZ):
    """
    فیلتر های‌پس یک‌قطبی ساده:
      - اول یک low-pass یک‌قطبی می‌سازد
      - high = x - low
    برای حذف هوم و فرکانس‌های خیلی پایین.
    """

    if sampwidth != 2:
        # فعلاً فقط برای 16-bit
        return pcm

    import math

    # تبدیل بایت‌ها به آرایه int16
    length = len(pcm)
    num_samples = length // 2
    # از array ماژول استاندارد استفاده کنیم تا نیاز به numpy نباشد
    import array
    x = array.array("h")
    x.frombytes(pcm)

    # پارامتر فیلتر
    dt = 1.0 / float(rate)
    rc = 1.0 / (2.0 * math.pi * float(cutoff_hz))  # RC = 1/(2πf_c)
    alpha = dt / (rc + dt)                         # برای low-pass

    low_prev = 0.0
    y = array.array("h")

    for n in range(num_samples):
        xn = float(x[n])
        low = low_prev + alpha * (xn - low_prev)  # low-pass
        high = xn - low                           # high-pass
        # کلیپ به بازه int16
        if high > 32767:
            high = 32767
        elif high < -32768:
            high = -32768
        y.append(int(high))
        low_prev = low

    return y.tobytes()


# ---------- گیت سکوت واقعی (فقط پنجره‌های خیلی کم‌صدا) ----------
def gate_true_silence(pcm, sampwidth, rate,
                      rms_threshold=SILENCE_RMS_THRESHOLD,
                      window_ms=SILENCE_WINDOW_MS):
    """
    فقط پنجره‌هایی که RMS آن‌ها از rms_threshold کمتر است صفر می‌شوند.
    این فقط سکوت واقعی/خیلی آرام را می‌زند، نه گفتار را.
    """

    if sampwidth != 2:
        return pcm

    frame_bytes = sampwidth  # mono → 2 بایت در هر سمپل
    window_samples = int(rate * window_ms / 1000.0)
    if window_samples <= 0:
        return pcm

    window_bytes = window_samples * frame_bytes
    length = len(pcm)
    out = bytearray(pcm)

    for start in range(0, length, window_bytes):
        end = min(start + window_bytes, length)
        chunk = pcm[start:end]
        if not chunk:
            break
        rms = audioop.rms(chunk, sampwidth)
        if rms < rms_threshold:
            out[start:end] = b"\x00" * (end - start)

    return bytes(out)


# ---------- ساخت نسخه کم‌حجم 16k/mono + high-pass + gate سکوت ----------
def make_small_wav(path_in, path_out,
                   target_rate=TARGET_RATE,
                   target_channels=TARGET_CHANNELS):
    if not os.path.exists(path_in):
        print(f"[!] File {path_in} does not exist, skipping small wav.")
        return

    with wave.open(path_in, "rb") as r:
        nch = r.getnchannels()
        sw = r.getsampwidth()
        fr = r.getframerate()
        nframes = r.getnframes()
        frames = r.readframes(nframes)

    print(f"[*] Compressing {path_in}: {nch}ch, {fr}Hz -> {target_channels}ch, {target_rate}Hz")

    # ۱) تبدیل استریو به مونو اگر لازم باشد
    if nch == 2 and target_channels == 1:
        frames_mono = audioop.tomono(frames, sw, 0.5, 0.5)
        nch_in = 1
    else:
        frames_mono = frames
        nch_in = nch

    # ۲) تغییر نرخ نمونه‌برداری (مثلاً 48000 -> 16000)
    converted, _ = audioop.ratecv(
        frames_mono,
        sw,
        nch_in,
        fr,
        target_rate,
        None
    )

    # ۳) های‌پس برای حذف هوم/بم خیلی پایین
    hp = highpass_filter(
        converted,
        sampwidth=sw,
        rate=target_rate,
        cutoff_hz=HIGHPASS_CUTOFF_HZ,
    )

    # ۴) گیت سکوت واقعی → فقط پنجره‌های خیلی کم‌صدا صفر می‌شوند
    gated = gate_true_silence(
        hp,
        sampwidth=sw,
        rate=target_rate,
        rms_threshold=SILENCE_RMS_THRESHOLD,
        window_ms=SILENCE_WINDOW_MS,
    )

    # ۵) ذخیره در فایل نهایی
    with wave.open(path_out, "wb") as w:
        w.setnchannels(target_channels)
        w.setsampwidth(sw)
        w.setframerate(target_rate)
        w.writeframes(gated)

    print(f"[*] Final wav written to {path_out} (16k/mono + high-pass + soft gate).")


def main():
    # 1) تنظیم میکسر
    configure_mixer()

    # 2) اجرای tinycap
    proc = subprocess.Popen(
        CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    print(f"[*] tinycap started with PID {proc.pid}")
    print("[*] Press ENTER to stop recording...")

    t = threading.Thread(target=stream_output, args=(proc,), daemon=True)
    t.start()

    # 3) منتظر ENTER می‌مانیم
    try:
        input()
        print("\n[!] Stop requested, stopping tinycap...")
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C detected, stopping tinycap...")

    # 4) بستن پروسه
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        print("[!] tinycap did not exit on SIGTERM, sending SIGKILL...")
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)

    print("[*] tinycap process stopped, fixing WAV header...")
    fix_wav_header(RAW_FILENAME, NATIVE_CHANNELS, NATIVE_RATE, SAMPLE_WIDTH)

    # 5) ساخت نسخه کم‌حجم + high-pass + gate سکوت
    make_small_wav(RAW_FILENAME, FINAL_FILENAME)

    # 6) پاک کردن فایل خام، فقط نسخه 16k را نگه دار
    try:
        os.remove(RAW_FILENAME)
        print(f"[*] Removed raw file {RAW_FILENAME}.")
    except FileNotFoundError:
        pass

    print("[*] Done. Final file:", FINAL_FILENAME)


if __name__ == "__main__":
    main()

