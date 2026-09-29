{
  description = "Gnosis VPN test environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    treefmt-nix.url = "github:numtide/treefmt-nix";

    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs:
    inputs.flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [ inputs.treefmt-nix.flakeModule ];
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      perSystem =
        { config, pkgs, ... }:
        {
          treefmt = {
            projectRootFile = "justfile";

            programs.nixfmt.enable = true;

            programs.deno.enable = true;
            settings.formatter.deno.excludes = [
              "*.toml"
              "*.yml"
              "*.yaml"
            ];

            programs.shellcheck.enable = true;
            # scripts source siblings through $SCRIPT_DIR/$HERE, unresolvable without these two
            programs.shellcheck.external-sources = true;
            programs.shellcheck.source-path = "SCRIPTDIR";

            programs.shfmt.enable = true;
            programs.shfmt.indent_size = 4; # every script here is 4-space; the module default is 2
            # `-s` rewrites "\$X" and "\\" into single quotes, which shellcheck then flags
            programs.shfmt.simplify = false;
          };

          # Host-mode e2e (`just e2e --client-mode host`) drives obscura on this machine
          # rather than in a sidecar: a container cannot join a macOS host's network
          # namespace, so the browser has to run where the tunnel routes actually live.
          devShells.default = pkgs.mkShell {
            packages =
              with pkgs;
              [
                (import ./nix/obscura.nix {
                  inherit pkgs;
                  lib = pkgs.lib;
                })
                nodejs
                gettext
                jq
                just
                bats
                opentelemetry-collector
                victoriametrics
                wireguard-tools
                config.treefmt.build.wrapper
              ]
              ++ pkgs.lib.attrValues config.treefmt.build.programs;
          };

          formatter = config.treefmt.build.wrapper;
        };
    };
}
