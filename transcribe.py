#!/usr/bin/env python3
"""Local transcription of OBS videos (mkv/mp4) into Markdown for Claude.

Pipeline: ffmpeg (16 kHz mono audio extraction) → whisper.cpp (whisper-cli, Vulkan/CUDA
GPU or CPU) → formatting into timestamped paragraphs.

Dependencies: python3 (standard library only), ffmpeg/ffprobe and whisper-cli on the PATH.
The launchers provide them: ./transcribe (Nix), ./transcribe-debian (Debian/Ubuntu),
transcribe-windows.cmd (Windows).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"

DEFAULT_MODEL = os.environ.get("VIT_MODEL", "large-v3-turbo")
DEFAULT_LANG = os.environ.get("VIT_LANG", "auto")
IS_WINDOWS = os.name == "nt"


def _app_dir(xdg_var: str, xdg_default: Path) -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "obs-transcribe"
    return Path(os.environ.get(xdg_var) or xdg_default) / "obs-transcribe"


DEFAULT_MODELS_DIR = Path(
    os.environ.get("VIT_MODELS_DIR")
    or _app_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "models"
)
CACHE_DIR = _app_dir("XDG_CACHE_HOME", Path.home() / ".cache") / ("cache" if IS_WINDOWS else "")

WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
VAD_MODEL_FILE = "ggml-silero-v5.1.2.bin"
VAD_MODEL_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/" + VAD_MODEL_FILE

KNOWN_MODELS = (
    "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo",
    "large-v3-turbo-q5_0", "large-v3-q5_0", "medium-q5_0",
)

PARAGRAPH_GAP_MS = 1500        # a silence this long between two segments starts a new paragraph
PARAGRAPH_MAX_CHARS = 900      # past this length, break at the end of the next sentence
MAX_CONSECUTIVE_REPEATS = 2    # Whisper sometimes loops: keep at most 2 identical segments in a row

# Phrases Whisper tends to make up over silence or music.
HALLUCINATION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"sous-?titr(es|age)s? (réalisés?|faits?|par)",
        r"amara\.org",
        r"merci d'avoir regardé",
        r"abonnez-vous",
        r"n'h[ée]sitez pas [àa] (vous )?abonner",
        r"thanks? for watching",
        r"subtitles? (by|provided)",
        r"^\s*[\[\(].*[\]\)]\s*$",   # [Music], (Laughter), [Applause]...
        r"^[\W_]*$",                  # symbols only: ♪ … -
    )
]


class Fatal(Exception):
    """Expected error, printed without a traceback."""


# Absolute paths of the external tools, filled by check_dependencies().
TOOLS: dict[str, str] = {}


# --------------------------------------------------------------------------- output

def info(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def progress(msg: str) -> None:
    print("\r\033[K" + msg, end="", file=sys.stderr, flush=True)


def setup_console() -> None:
    """Windows: UTF-8 output even when piped, and ANSI escapes for the progress line."""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        pass


def fmt_ts(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    if h:
        return f"{h} h {m:02d} min {sec:02d} s"
    if m:
        return f"{m} min {sec:02d} s"
    return f"{sec} s"


# --------------------------------------------------------------------------- models

def download(url: str, dest: Path, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    info(f"Downloading {label} to {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": f"obs-transcribe/{VERSION}"})
    try:
        with urllib.request.urlopen(req) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 0.5 or done == total:
                    last = now
                    if total:
                        progress(f"  {done / 1e6:.0f} / {total / 1e6:.0f} MB ({done * 100 // total}%)")
                    else:
                        progress(f"  {done / 1e6:.0f} MB")
        info()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        part.unlink(missing_ok=True)
        raise Fatal(f"download failed ({e}): {url}") from None
    part.replace(dest)


def ensure_model(models_dir: Path, name: str) -> Path:
    path = models_dir / f"ggml-{name}.bin"
    if not path.exists():
        if name not in KNOWN_MODELS:
            info(f'Unknown model "{name}", trying to download it anyway '
                 f"(common models: {', '.join(KNOWN_MODELS)})")
        download(WHISPER_MODEL_URL.format(name=name), path, f"Whisper model {name}")
    return path


def ensure_vad_model(models_dir: Path) -> Path:
    path = models_dir / VAD_MODEL_FILE
    if not path.exists():
        download(VAD_MODEL_URL, path, "Silero VAD model")
    return path


# --------------------------------------------------------------------------- external tools

def check_dependencies() -> None:
    missing = []
    for tool in ("ffmpeg", "ffprobe", "whisper-cli"):
        path = os.environ.get("VIT_" + tool.upper().replace("-", "_")) or shutil.which(tool)
        if path is None:
            missing.append(tool)
        else:
            TOOLS[tool] = path
    if missing:
        raise Fatal(
            f"missing tool(s): {', '.join(missing)}. Run the script through its launcher: "
            '"./transcribe" (NixOS / Nix), "./transcribe-debian" (Debian / Ubuntu) '
            'or "transcribe-windows" (Windows).'
        )


def pick_gpu_device() -> tuple[int, str, str] | None | bool:
    """Pick the GPU: the dedicated one (NVIDIA/AMD) rather than an integrated Intel GPU.

    whisper-cli uses device 0 by default, which is often the iGPU on a laptop. Return
    (index, name, backend); False if whisper-cli only sees software renderers (llvmpipe),
    so the caller should force the CPU; None if no GPU backend was detected.
    """
    try:
        r = subprocess.run([TOOLS["whisper-cli"], "--help"], capture_output=True, text=True,
                           errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stderr + r.stdout
    # Vulkan: "ggml_vulkan: 1 = NVIDIA GeForce GTX 1660 Ti (NVIDIA) | uma: 0 | ..."
    # CUDA:   "  Device 0: NVIDIA GeForce GTX 1660 Ti, compute capability 7.5, VMM: yes"
    devices = [(int(i), n, "Vulkan") for i, n in re.findall(r"ggml_vulkan: (\d+) = (.+?) \|", out)]
    devices += [(int(i), n, "CUDA") for i, n in re.findall(r"Device (\d+): (.+?), compute capability", out)]
    if not devices:
        return None
    usable = [d for d in devices if not re.search(r"llvmpipe|lavapipe|swiftshader", d[1], re.I)]
    if not usable:
        return False
    discrete = [d for d in usable if re.search(r"nvidia|amd|radeon|arc", d[1], re.I)]
    index, name, backend = (discrete or usable)[0]
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name)  # drop the driver name, e.g. "(NVIDIA)"
    return index, name, backend


def probe(video: Path) -> tuple[float, int]:
    """Return (duration in seconds, number of audio tracks). Raise Fatal if unreadable."""
    cmd = [TOOLS["ffprobe"], "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(video)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["ffprobe failed"])[-1]
        hint = ""
        if "moov" in r.stderr:
            hint = ('. Interrupted OBS recording: the mp4 has no "moov atom". '
                    "In OBS, record to mkv, with automatic remux if you want an mp4 at the end.")
        raise Fatal(f"unreadable file: {err}{hint}")
    data = json.loads(r.stdout)
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise Fatal("no audio track in this file")
    duration = float(data.get("format", {}).get("duration") or 0)
    return duration, len(audio)


def extract_audio(video: Path, wav: Path, n_tracks: int, track: int | None) -> None:
    cmd = [TOOLS["ffmpeg"], "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(video)]
    if track is not None:
        if not 1 <= track <= n_tracks:
            raise Fatal(f"audio track {track} does not exist (the file has {n_tracks})")
        cmd += ["-map", f"0:a:{track - 1}"]
    elif n_tracks > 1:
        # Multi-track OBS recording (mic + desktop audio...): mix all tracks.
        inputs = "".join(f"[0:a:{i}]" for i in range(n_tracks))
        cmd += ["-filter_complex", f"{inputs}amix=inputs={n_tracks}:duration=longest[a]", "-map", "[a]"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
        raise Fatal(f"audio extraction failed: {err}")


def run_whisper(
    wav: Path,
    prefix: Path,
    *,
    model_path: Path,
    lang: str,
    threads: int,
    vad_path: Path | None,
    device: int | None,
    cpu: bool,
    prompt: str | None,
    srt: bool,
) -> dict:
    cmd = [
        TOOLS["whisper-cli"], "-m", str(model_path), "-f", str(wav), "-l", lang, "-t", str(threads),
        "-oj", "-of", str(prefix), "-np", "-pp",
    ]
    if vad_path is not None:
        cmd += ["--vad", "-vm", str(vad_path)]
    if cpu:
        cmd += ["-ng"]
    elif device is not None:
        cmd += ["-dev", str(device)]
    if prompt:
        cmd += ["--prompt", prompt]
    if srt:
        cmd += ["-osrt"]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    log: list[str] = []
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            log.append(line)
            m = re.search(r"progress\s*=\s*(\d+)%", line)
            if m:
                progress(f"  transcribing... {int(m.group(1)):3d}%")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    progress("")
    if proc.returncode != 0:
        tail = "".join(log[-15:]).rstrip()
        raise Fatal(f"whisper-cli failed (exit code {proc.returncode}):\n{tail}")
    with open(f"{prefix}.json", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- formatting

def is_hallucination(text: str) -> bool:
    return any(p.search(text) for p in HALLUCINATION_PATTERNS)


def build_paragraphs(segments: list[dict]) -> list[tuple[int, str]]:
    """Group Whisper segments into paragraphs (start in ms, text)."""
    paragraphs: list[tuple[int, str]] = []
    cur_start, cur_end, cur_parts, cur_len = 0, 0, [], 0
    prev_norm, repeats = None, 0

    for seg in segments:
        text = " ".join(seg.get("text", "").split())
        if not text or is_hallucination(text):
            continue
        norm = re.sub(r"[\W_]+", " ", text).strip().lower()
        if norm == prev_norm:
            repeats += 1
            if repeats >= MAX_CONSECUTIVE_REPEATS:
                continue
        else:
            prev_norm, repeats = norm, 0

        start, end = int(seg["offsets"]["from"]), int(seg["offsets"]["to"])
        new_para = bool(cur_parts) and (
            start - cur_end >= PARAGRAPH_GAP_MS
            or (cur_len >= PARAGRAPH_MAX_CHARS and cur_parts[-1].endswith((".", "!", "?", "…")))
        )
        if new_para:
            paragraphs.append((cur_start, " ".join(cur_parts)))
            cur_parts, cur_len = [], 0
        if not cur_parts:
            cur_start = start
        cur_parts.append(text)
        cur_len += len(text) + 1
        cur_end = end

    if cur_parts:
        paragraphs.append((cur_start, " ".join(cur_parts)))
    return paragraphs


def recorded_at(video: Path) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[_ ](\d{2})-(\d{2})-(\d{2})", video.name)
    if m:
        try:
            when = dt.datetime(*map(int, m.groups()))
            return when.strftime("%Y-%m-%d %H:%M") + " (from the file name)"
        except ValueError:
            pass
    when = dt.datetime.fromtimestamp(video.stat().st_mtime)
    return when.strftime("%Y-%m-%d %H:%M") + " (file modification time)"


def render_markdown(
    video: Path,
    duration: float,
    language: str,
    lang_forced: bool,
    model: str,
    paragraphs: list[tuple[int, str]],
    timestamps: bool,
) -> str:
    lines = [
        f"# Transcript: {video.name}",
        "",
        f"- Source: `{video.resolve()}`",
        f"- Recorded: {recorded_at(video)}",
        f"- Duration: {fmt_duration(duration)}",
        ("- Language: unknown (no speech detected)" if not paragraphs
         else f"- Language: {language}" + ("" if lang_forced else " (auto-detected)")),
        f"- Model: whisper.cpp · {model}",
        f"- Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} by transcribe v{VERSION}",
        "",
        "---",
        "",
    ]
    if not paragraphs:
        lines.append("_(no speech detected)_")
    for start, text in paragraphs:
        lines.append(f"[{fmt_ts(start)}] {text}" if timestamps else text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- processing

def process(video: Path, args: argparse.Namespace, model_path: Path, vad_path: Path | None,
            device: int | None) -> bool:
    """Transcribe one video. Return False if it was skipped (already transcribed)."""
    if not video.is_file():
        raise Fatal("file not found")
    out_dir = args.out_dir or video.parent
    out_md = out_dir / (video.stem + ".md")
    out_srt = out_dir / (video.stem + ".srt")
    if not args.stdout and out_md.exists() and not args.force:
        info(f"↷ {video.name}: {out_md.name} already exists (use --force to redo it)")
        return False

    duration, n_tracks = probe(video)
    info(f"▶ {video.name}  ({fmt_duration(duration)}"
         + (f", {n_tracks} audio tracks mixed" if n_tracks > 1 and args.track is None else "") + ")")
    t0 = time.monotonic()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="transcribe-", dir=CACHE_DIR) as tmp:
        tmp_dir = Path(tmp)
        wav = tmp_dir / "audio.wav"
        progress("  extracting audio...")
        extract_audio(video, wav, n_tracks, args.track)
        progress("  transcribing...   0%")
        result = run_whisper(
            wav, tmp_dir / "out", model_path=model_path, lang=args.lang, threads=args.threads,
            vad_path=vad_path, device=device, cpu=args.cpu, prompt=args.prompt, srt=args.srt,
        )
        if args.srt:
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir / "out.srt"), out_srt)

    language = (result.get("result") or {}).get("language") or args.lang
    paragraphs = build_paragraphs(result.get("transcription") or [])
    md = render_markdown(video, duration, language, args.lang != "auto", args.model, paragraphs,
                         timestamps=not args.no_timestamps)

    lang_label = language if paragraphs else "no speech"
    elapsed = time.monotonic() - t0
    ratio = duration / elapsed if elapsed > 0 and duration else 0
    # One decimal below 10x: on a CPU the ratio is often under 1 and ".0f" printed "0x"
    speed = f", {ratio:.{0 if ratio >= 10 else 1}f}x real time" if ratio else ""
    if args.stdout:
        sys.stdout.write(md)
        sys.stdout.flush()
        info(f"✓ transcribed ({lang_label}, {fmt_duration(elapsed)}{speed})")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
        words = sum(len(t.split()) for _, t in paragraphs)
        info(f"✓ {out_md}  ({lang_label}, {words} words, {fmt_duration(elapsed)}{speed})")
    return True


def expand_files(files: list[Path]) -> list[Path]:
    """Expand ~ and wildcards ourselves: cmd.exe and PowerShell pass "*.mp4" through as is."""
    out: list[Path] = []
    for f in files:
        f = f.expanduser()
        if not f.exists() and glob.has_magic(str(f)):
            matches = sorted(glob.glob(str(f)))
            if matches:
                out.extend(Path(m) for m in matches)
                continue
        out.append(f)
    return out


def default_threads() -> int:
    return max(1, min(8, (os.cpu_count() or 4) // 2))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="transcribe",
        description="Transcribe videos locally (OBS mkv/mp4, or any file ffmpeg can read) "
                    "into Markdown ready to hand to Claude. Engine: whisper.cpp (GPU or CPU).",
        epilog="Environment variables: VIT_MODEL, VIT_LANG, VIT_MODELS_DIR, VIT_OUT_DIR, "
               "VIT_DEVICE, VIT_PROMPT (they override the defaults of the options above).\n"
               "VIT_FFMPEG, VIT_FFPROBE, VIT_WHISPER_CLI: paths of the tools [found on the PATH].\n"
               "Examples:\n"
               "  transcribe ~/Videos/2026-09-24_14-05-02.mp4\n"
               "  transcribe -l fr --srt ~/Videos/*.mp4\n"
               "  transcribe --stdout meeting.mkv | claude -p 'Summarize this meeting'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="+", type=Path, metavar="VIDEO", help="video (or audio) files to transcribe")
    p.add_argument("-l", "--lang", default=DEFAULT_LANG,
                   help='spoken language (fr, en, ...) or "auto" to detect it [%(default)s]')
    p.add_argument("-m", "--model", default=DEFAULT_MODEL,
                   help="ggml Whisper model, downloaded on first use [%(default)s]")
    p.add_argument("-o", "--out-dir", type=Path, default=os.environ.get("VIT_OUT_DIR") or None,
                   help="output directory [next to the video]")
    p.add_argument("--stdout", action="store_true", help="write the Markdown to standard output instead of a file")
    p.add_argument("--srt", action="store_true", help="also write .srt subtitles")
    p.add_argument("--no-timestamps", action="store_true", help="no [HH:MM:SS] timestamp before each paragraph")
    p.add_argument("--prompt", default=os.environ.get("VIT_PROMPT") or None,
                   help="initial prompt for Whisper (vocabulary, proper nouns, punctuation style)")
    p.add_argument("--track", type=int, metavar="N", help="use only audio track N (1 = first) instead of mixing all tracks")
    p.add_argument("--no-vad", action="store_true", help="disable voice activity detection (VAD), which skips silences")
    p.add_argument("--cpu", action="store_true", help="force the CPU (no GPU)")
    p.add_argument("--device", type=int, metavar="N",
                   default=int(os.environ["VIT_DEVICE"]) if os.environ.get("VIT_DEVICE") else None,
                   help="GPU index (Vulkan or CUDA) [auto: dedicated GPU if present]")
    p.add_argument("-t", "--threads", type=int, default=default_threads(), help="CPU threads [%(default)s]")
    p.add_argument("-f", "--force", action="store_true", help="transcribe again even if the .md already exists")
    p.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR, help="model directory [%(default)s]")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    args.files = expand_files(args.files)
    try:
        check_dependencies()
        model_path = ensure_model(args.models_dir, args.model)
        vad_path = None if args.no_vad else ensure_vad_model(args.models_dir)

        device: int | None = None
        if args.cpu:
            info("Compute: CPU")
        elif args.device is not None:
            device = args.device
            info(f"Compute: GPU #{device}")
        else:
            picked = pick_gpu_device()
            if picked is None:
                info("Compute: no GPU found, using the CPU")
            elif picked is False:
                args.cpu = True  # only a software Vulkan renderer: slower than the CPU backend
                info("Compute: no hardware GPU found (only a software Vulkan renderer), using the CPU")
            else:
                device = picked[0]
                info(f"Compute: {picked[1]} ({picked[2]} #{device})")

        failures = 0
        for video in args.files:
            try:
                process(video, args, model_path, vad_path, device)
            except Fatal as e:
                failures += 1
                progress("")  # clear any progress line still on screen
                info(f"✗ {video.name}: {e}")
        return 1 if failures else 0
    except Fatal as e:
        info(f"Error: {e}")
        return 2
    except KeyboardInterrupt:
        info("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
