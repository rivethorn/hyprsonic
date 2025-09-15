#!/usr/bin/env python3
"""
Low-latency keyboard sound player using pyminiaudio + evdev.
Mixes sounds in-process so keypresses play immediately and can overlap.
"""

# TODO!
# Get and copy the audio files on first run.

import miniaudio
from evdev import InputDevice, list_devices, ecodes
import threading
import random
import sys
import os
from array import array

# ----------------- Configuration -----------------
# Paths to audio files (prefer 16-bit PCM WAVs, same sample rate & channels if possible)
BASE = os.path.expanduser("~/.local/share/hyprsonic")
KeyUps_paths = [
    os.path.join(BASE, "fallback-up.wav"),
    os.path.join(BASE, "fallback2-up.wav"),
]
KeyDowns_paths = [
    os.path.join(BASE, "fallback.wav"),
    os.path.join(BASE, "fallback2.wav"),
]
BackUp_path = os.path.join(BASE, "backspace-up.wav")
BackDown_path = os.path.join(BASE, "backspace.wav")
EnterUp_path = os.path.join(BASE, "enter-up.wav")
EnterDown_path = os.path.join(BASE, "enter.wav")
SpaceUp_path = os.path.join(BASE, "spacebar-up.wav")
SpaceDown_path = os.path.join(BASE, "spacebar.wav")

# Desired output format for the audio device + decoded data
OUT_SAMPLE_RATE = 48000
OUT_CHANNELS = 2
OUT_FORMAT = miniaudio.SampleFormat.SIGNED16  # 16-bit signed PCM


# ----------------- Utility: find keyboard device -----------------
def find_first_keyboard():
    devs = [InputDevice(p) for p in list_devices()]
    for d in devs:
        caps = d.capabilities().get(ecodes.EV_KEY, [])
        if caps:
            return d
    return None


kbd = find_first_keyboard()
if not kbd:
    print("No keyboard device found. Exiting.")
    sys.exit(1)

print("Using devices:", kbd.path, kbd.name)

# ----------------- Preload sounds (decoded to desired format) -----------------
def load_sound(path):
    """Retrun a dict with 'sample' (array('h')), 'nchannels', 'sample_rate' and 'nframes'."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    # decode_file return a DecodedSoundFile
    dsf = miniaudio.decode_file(
        path,
        output_format=OUT_FORMAT,
        nchannels=OUT_CHANNELS,
        sample_rate=OUT_SAMPLE_RATE
    )
    # dsf.samples is an array of signed integers (interleaved if nchannels > 1)
    # dsf.nframes possibly exists (frame = sample / nchannels)
    samples = dsf.samples # array('h') for SIGNED16
    nframes = len(samples) // OUT_CHANNELS
    return {
        "samples": samples,
        "nchannels": OUT_CHANNELS,
        "sample_rate": OUT_SAMPLE_RATE,
        "nframes": nframes,
    }

# Load all sounds
try:
    KeyUps = [load_sound(p) for p in KeyUps_paths]
    KeyDowns = [load_sound(p) for p in KeyDowns_paths]
    BackUp = load_sound(BackUp_path)
    BackDown = load_sound(BackDown_path)
    EnterUp = load_sound(EnterUp_path)
    EnterDown = load_sound(EnterDown_path)
    SpaceUp = load_sound(SpaceUp_path)
    SpaceDown = load_sound(SpaceDown_path)
except FileNotFoundError as e:
    print("Missing sound file:", e)
    sys.exit(1)

# ----------------- Mixer state -----------------
active_sounds = []
active_lock = threading.Lock()

def enqueue_sound(dsf, volume=1.0):
    """Add a decoded sound (dsf dict) to the active list so mixer will play it."""
    # copy reference to samples; position measured on sample (not frames) index
    with active_lock:
        active_sounds.append({
            "samples": dsf["samples"],
            "pos": 0,
            "vol": float(volume),
            "nframes": dsf["nframes"],
        })

# ----------------- Mixer generatior for miniaudio device -----------------
def mixer_generator():
    """
    Generator that yields interleaved PCM frames as required by the playback device.
    The device will call it with required_frames (number of frames requested).
    We return array.array('h') or bytes.
    """
    required_frames = yield b"" # initialize generator; first yield receives frame request
    channels = OUT_CHANNELS
    max_amp = 32767
    min_amp = -32768
    
    # We'll buffer in a Python array('i') (32-bit) for accumulation to avoid overflow,
    # then convert/clip to array('h') for output.
    while True:
        # Create accumulation buffer of zeros (length = required_frames * channels)
        acc = [0] * (required_frames * channels) # using list of ints is faster for small buffers
        remove_list = []
        with active_lock:
            for s in active_sounds:
                samples = s["samples"]
                pos = s["pos"]
                vol = s["vol"]
                # number of samples requested (interleaved samples)
                want = required_frames * channels
                # slice available samples
                available = len(samples) - pos
                take = want if available >= want else available
                if take > 0:
                    # mix: add sample * vol to acc
                    # samples is array('h'), support slicing efficiently but yields array
                    chunk = samples[pos:pos + take]
                    # loop add
                    for i in range(take):
                        acc[i] += int(chunk[i] * vol)
                    s["pos"] += take
                # if we've exhausted this sample, mark for removal
                if s["pos"] >= len(s["samples"]):
                    remove_list.append(s)
        # remove finished sounds
        if remove_list:
            with active_lock:
                for r in remove_list:
                    if r in active_sounds: active_sounds.remove(r)
        
        # clip to int16 and produce array('h')
        out_arr = array('h', [0]) * (required_frames * channels)  # pre-allocated
        for i,v in enumerate(acc):
            if v > max_amp:
                v = max_amp
            elif v < min_amp:
                v = min_amp
            out_arr[i] = int(v)
        # yield bytes for playback device
        required_frames = yield out_arr.tobytes()

# ----------------- Start playback device -----------------
playback_device = miniaudio.PlaybackDevice(
    output_format=OUT_FORMAT,
    nchannels=OUT_CHANNELS,
    sample_rate=OUT_SAMPLE_RATE,
    buffersize_msec=10
)
stream = mixer_generator()
next(stream)
playback_device.start(stream)
print("Audio playback started (mixer).")

# ----------------- evdev loop (main thread) -----------------
# Minimal prints to avoid jitter
try:
    for ev in kbd.read_loop():
        if ev.type != ecodes.EV_KEY:
            continue
        key = ecodes.KEY.get(ev.code, str(ev.code))
        if ev.value == 1:  # key down
            if "ENTER" in key:
                enqueue_sound(EnterDown)
            elif "BACK" in key:
                enqueue_sound(BackDown)
            elif "SPACE" in key:
                enqueue_sound(SpaceDown)
            else:
                enqueue_sound(random.choice(KeyDowns))
        elif ev.value == 0:  # key up
            if "ENTER" in key:
                enqueue_sound(EnterUp)
            elif "BACK" in key:
                enqueue_sound(BackUp)
            elif "SPACE" in key:
                enqueue_sound(SpaceUp)
            else:
                enqueue_sound(random.choice(KeyUps))
except KeyboardInterrupt:
    print("\nStopping...")
finally:
    playback_device.stop()
    playback_device.close()