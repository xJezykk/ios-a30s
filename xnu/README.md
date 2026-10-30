# XNU for the Galaxy A30s (SM-A307FN, Exynos 7904)

Built on GitHub's macOS arm64 runners by `.github/workflows/xnu.yml`, using
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build) (MIT) at a pinned commit.

- The workflow fixes `MACOS_VERSION=13.5` / `XNU_VERSION=xnu-8796.141.3`
  at commit `1b191cb58250d0705d8a51287127505aa4bc0789`,
  `DEVELOPMENT`, `ARM64`, and `SWEET`; dispatch runs cannot silently select
  another source or target.
- `patches/` in this repo carries **only the Exynos patches of this
  project** (`0082` onward). The upstream ios-sweet series (0001-0081,
  MIT) is fetched by the workflow at build time from a **pinned commit**
  and merged in glob order (`0001..0081` then ours `0082+`) - so no
  Apple-authored source text is mirrored in this repository.
- Patch 0084 is verified in the patched source tree after the build. The
  workflow also checks the build log for the ARMv8-A target flag and scans
  ARM64 Mach-O executable instruction sections for decoded LSE operations
  before publishing any kernel artifact.
- Output: the verified `kernel.development.sweet` and provenance/hash files
  in artifact `xnu-kernel-<workflow commit SHA>`. The workflow does not build
  a SWEETXNU payload, shim image, or Android boot image. For that next stage,
  download this exact artifact and pass its kernel, `tools/mkadt-a30s.py`
  output, and any intended ramdisk to `tools/mkpayload.py`; then build the
  shim with the resulting payload. Do not package a checked-in or older
  kernel in place of the downloaded artifact.

Fetch a run's kernel artifact: `gh run download -R <owner>/ios-a30s -n xnu-kernel-<workflow-commit-sha> -D xnu/out`
