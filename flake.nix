{
  description = "Local transcription of OBS videos (mkv/mp4) into Markdown for Claude, using whisper.cpp and ffmpeg";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        transcribe = pkgs.callPackage ./nix/transcribe.nix { };
        default = self.packages.${pkgs.stdenv.hostPlatform.system}.transcribe;
      });

      # nix develop: ffmpeg + whisper-cli + python3 on the PATH, to run ./transcribe.py by hand
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.whisper-cpp-vulkan
            pkgs.ffmpeg-headless
            pkgs.python3
          ];
        };
      });
    };
}
