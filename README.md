# obs-transcribe

Turns OBS recordings (`.mkv`, `.mp4`, or any file ffmpeg can read) into a Markdown transcript you can
hand to Claude, **100% locally**. Nothing leaves the machine, and there is no quota or length limit.

Engine: [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (a C/C++ port of OpenAI's Whisper
model, MIT license) running the `large-v3-turbo` model on the GPU (Vulkan or CUDA) or the CPU.
The `transcribe.py` script (plain Python, no dependencies) chains ffmpeg → whisper.cpp → formatting.

## Launchers

`transcribe.py` is the same everywhere; one launcher per system provides ffmpeg, whisper.cpp and
Python, then runs it with your arguments:

| System | Command | First run |
|---|---|---|
| NixOS, or any Linux with Nix | `./transcribe` | Nix fetches whisper.cpp (Vulkan) and ffmpeg from the binary cache |
| Debian, Ubuntu | `./transcribe-debian` | Installs `python3`, `ffmpeg` and the build tools with apt (sudo), then builds whisper.cpp (a few minutes; Vulkan on Ubuntu 24.04+ / Debian 13, CPU on older releases) |
| Windows 10/11 | `transcribe-windows` (cmd or PowerShell, from the repository) | Downloads a portable Python, ffmpeg and the prebuilt whisper.cpp (CUDA build if there is an NVIDIA GPU, CPU build otherwise) |

All three take the same options; the examples below use `./transcribe`.

### Debian / Ubuntu

```sh
git clone https://github.com/typovrak/obs-transcribe && cd obs-transcribe
./transcribe-debian ~/Videos/2026-09-24_14-05-02.mp4
```

whisper.cpp is built once into `~/.local/share/obs-transcribe/whisper.cpp-<version>-<backend>/`.
Vulkan uses the GPU with the NVIDIA driver as well as with Mesa (AMD, Intel), and needs Ubuntu
24.04 or newer (or Debian 13). Ubuntu 22.04 has no `glslc` package and Debian 12's Vulkan headers
are too old, so there the build is CPU-only; `TRANSCRIBE_GPU=cuda ./transcribe-debian …` builds for
CUDA instead if the CUDA toolkit is installed. If the Vulkan build fails anyway, the launcher falls
back to a CPU build. Tested in Debian 12, Ubuntu 22.04 and Ubuntu 24.04 containers (without a GPU). To rebuild, delete that directory (or set `TRANSCRIBE_GPU=vulkan|cuda|cpu` to build
another backend next to it).

### Windows

Download the repository (`git clone`, or *Code → Download ZIP* on GitHub), open a terminal in it:

```powershell
.\transcribe-windows "C:\Users\me\Videos\2026-09-24 14-05-02.mkv"
.\transcribe-windows -l fr C:\Users\me\Videos\*.mkv
```

Everything goes into `%LOCALAPPDATA%\obs-transcribe\` (Python, ffmpeg, whisper.cpp, models): no
administrator rights, nothing installed system-wide, delete that directory to uninstall. If
`ffmpeg` and `ffprobe` are already on the PATH (e.g. `winget install Gyan.FFmpeg`), they are used.
whisper.cpp publishes no Vulkan build for Windows, so AMD and Intel GPUs run on the CPU there;
`$env:TRANSCRIBE_GPU = "cpu"` (PowerShell) or `set TRANSCRIBE_GPU=cpu` (cmd) forces the CPU build on
an NVIDIA machine. `transcribe-windows.cmd` only starts
`windows\launcher.ps1` with `-ExecutionPolicy Bypass`, because PowerShell refuses to run `.ps1`
scripts by default.

## Usage

```sh
./transcribe ~/Videos/2026-09-24_14-05-02.mp4
# writes ~/Videos/2026-09-24_14-05-02.md

./transcribe ~/Videos/*.mp4            # transcribe everything; videos that already have a .md are skipped
./transcribe -l fr --srt meeting.mkv   # force French, also write .srt subtitles
./transcribe --stdout meeting.mkv | claude -p "Summarize this meeting in 10 points"
```

On the first run, the script downloads the Whisper model (1.6 GB) and the Silero voice activity
detection model into `~/.local/share/obs-transcribe/models/` (`%LOCALAPPDATA%\obs-transcribe\models\`
on Windows).

### Output

```markdown
# Transcript: 2026-09-24_14-05-02.mp4

- Source: `/home/typovrak/Videos/2026-09-24_14-05-02.mp4`
- Recorded: 2026-09-24 14:05 (from the file name)
- Duration: 55 min 50 s
- Language: fr (auto-detected)
- Model: whisper.cpp · large-v3-turbo
- Generated: 2026-09-24 17:12 by transcribe v0.1.0

---

[00:00:03] Bonjour à tous, on va commencer par …

[00:01:27] Oui, j'ai pu voir ça sur votre page Google Business, …
```

The transcript stays in the spoken language. A new paragraph starts after 1.5 s of silence, or at the
end of a sentence once the paragraph passes 900 characters. The timestamp in front of each paragraph
lets you ask Claude "when do we talk about X?" or find the passage in the video.

### Options

| Option | Effect |
|---|---|
| `-l, --lang LANG` | Spoken language (`fr`, `en`, …) or `auto` (default), detected from the first seconds of speech |
| `-m, --model MODEL` | Whisper model (default `large-v3-turbo`), downloaded on first use |
| `-o, --out-dir OUT_DIR` | Output directory (default: next to the video) |
| `--stdout` | Write the Markdown to standard output instead of a file |
| `--srt` | Also write `.srt` subtitles |
| `--no-timestamps` | Paragraphs without `[HH:MM:SS]` |
| `--prompt PROMPT` | Initial prompt for Whisper: proper nouns, technical vocabulary, punctuation style |
| `--track N` | Use only audio track N (multi-track OBS recordings) instead of mixing all tracks |
| `--no-vad` | Disable voice activity detection (VAD), which skips silences |
| `--cpu` / `--device N` | Force the CPU, or pick GPU N, Vulkan or CUDA (default: the dedicated GPU) |
| `-f, --force` | Transcribe again even if the `.md` already exists |

Environment variables override the same defaults: `VIT_MODEL`, `VIT_LANG`, `VIT_OUT_DIR`,
`VIT_MODELS_DIR`, `VIT_DEVICE`, `VIT_PROMPT`. `VIT_FFMPEG`, `VIT_FFPROBE` and `VIT_WHISPER_CLI`
point to the tools if they are not on the PATH.

## Measured performance

GTX 1660 Ti (6 GB), Vulkan, `large-v3-turbo` model, VAD on:

| Video | Length | Transcription time | Speed |
|---|---|---|---|
| Phone call | 3 min 34 s | 13 s | ≈ 16× real time |
| Video call | 30 min 32 s | 3 min 11 s | ≈ 10× real time |
| Recorded session (`--srt`) | 2 h 02 min | 11 min 23 s | ≈ 11× real time |

On the CPU alone (`--cpu`, 6 threads), `large-v3-turbo` runs at about real time and the `base` model
at about 3× real time. Keep the GPU.

## Models

| Model | Size | Use |
|---|---|---|
| `large-v3-turbo` (default) | 1.6 GB | Best trade-off: quality close to large-v3, 4× faster |
| `large-v3` | 3.1 GB | Highest accuracy, about 3× slower |
| `large-v3-turbo-q5_0` | 0.6 GB | Quantized version of the default, slightly less accurate, uses less VRAM |
| `medium`, `small`, `base`, `tiny` | 1.5 GB → 75 MB | Faster and faster, less and less reliable in French |

`./transcribe -m large-v3 video.mp4` or `VIT_MODEL=large-v3`. The models come from
[huggingface.co/ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/tree/main).

## Installing the `transcribe` command (Nix)

The `./transcribe` wrapper runs `nix run path:<this directory>`. To get `transcribe` on your PATH:

- Imperative: `nix profile install github:typovrak/obs-transcribe`
- Declarative, in the NixOS flake (`~/nixos`):

  ```nix
  inputs.obs-transcribe.url = "github:typovrak/obs-transcribe";
  # then, in the configuration:
  environment.systemPackages = [ inputs.obs-transcribe.packages.${pkgs.stdenv.hostPlatform.system}.default ];
  ```

- Without installing: `nix develop` puts `ffmpeg`, `whisper-cli` and `python3` on the PATH, then
  run `python3 transcribe.py …`.

To update whisper.cpp, run `nix flake update` (`flake.lock` pins the nixpkgs revision).

## Using it with Claude

- **claude.ai**: drag the `.md` into the conversation or into a project's files.
- **Claude Code**: `claude "Summarize @~/Videos/2026-09-24_14-05-02.md"`, or through a pipe:
  `./transcribe --stdout video.mp4 | claude -p "List the decisions and action items"`.
- In these recordings, one hour of speech came to 8,000 to 11,000 words, so even a 3-hour session
  fits in Claude's context window.

## Limitations and pitfalls

- **Corrupted OBS mp4 files:** an interrupted recording (crash, power loss) leaves an `.mp4` without
  a "moov atom", which makes it unreadable; the script reports it and moves on to the next file. In
  OBS, set *Settings → Output → Recording Format* to mkv: an mkv cut off mid-recording stays
  readable. The *Automatically remux to MP4* option then converts it to mp4 when the recording ends.
- **Language:** automatic detection looks at the first 30 seconds of speech. If a recording starts in
  a different language from the rest, force it with `-l fr`.
- **Silences and hallucinations:** the VAD (Silero) removes silences before Whisper runs. Transcription
  goes faster and Whisper makes up fewer sentences. The usual hallucinations ("Sous-titres réalisés par
  la communauté d'Amara.org", "Thanks for watching"…) and repetition loops are filtered in
  `transcribe.py` (`HALLUCINATION_PATTERNS`).
- **GPU:** on this laptop, Vulkan device 0 is the Intel iGPU and the 1660 Ti is device 1; the script
  picks the dedicated GPU automatically. If Vulkan only exposes a software renderer (llvmpipe, e.g.
  in a VM or a container), it uses the CPU backend, which is faster. With Nix, CUDA is not needed. To
  try it anyway, pass
  `whisperPackage = pkgs.whisper-cpp.override { cudaSupport = true; }` to the package through
  `.override` (long build).
- **Speakers:** Whisper does not know who is speaking. If you need that one day, look at
  [WhisperX](https://github.com/m-bain/whisperX) (pyannote, Hugging Face token, PyTorch + CUDA), or
  ask Claude to infer the speakers from context, which is simpler.

## Why whisper.cpp

| Tool | Verdict |
|---|---|
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Used here: no Python or CUDA needed, in the nixpkgs binary cache, GPU through Vulkan with the NVIDIA driver already installed, built-in VAD, 10 to 16× real time on this machine |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Same Whisper models on CTranslate2, but it needs Python, and GPU support requires CUDA/cuDNN, which means a long build on NixOS |
| [WhisperX](https://github.com/m-bain/whisperX) | Adds speaker identification; needs PyTorch, CUDA and a Hugging Face token. An option for later |
| [openai/whisper](https://github.com/openai/whisper) | Reference implementation in PyTorch, much slower |
