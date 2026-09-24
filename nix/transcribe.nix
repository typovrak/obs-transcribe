{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  ffmpeg-headless,
  whisper-cpp-vulkan,
  # Engine to use. Swap it with .override, e.g. for CUDA:
  #   whisperPackage = pkgs.whisper-cpp.override { cudaSupport = true; }
  # (the name must not exist in pkgs, or callPackage would inject that package instead of the default)
  whisperPackage ? whisper-cpp-vulkan,
}:

stdenvNoCC.mkDerivation {
  pname = "transcribe";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = ../transcribe.py;
  };

  nativeBuildInputs = [
    makeWrapper
    python3
  ];

  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 transcribe.py $out/bin/transcribe
    wrapProgram $out/bin/transcribe \
      --prefix PATH : ${lib.makeBinPath [ ffmpeg-headless whisperPackage ]}
    runHook postInstall
  '';

  meta = {
    description = "Local transcription of OBS videos (mkv/mp4) into Markdown for Claude, using whisper.cpp";
    mainProgram = "transcribe";
    platforms = lib.platforms.linux;
  };
}
