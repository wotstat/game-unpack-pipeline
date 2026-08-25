# JPEXS Free Flash Decompiler runtime tool

- Pinned version: `26.2.1` (see `Dockerfile`).
- Upstream: <https://github.com/jindrapetrik/jpexs-decompiler>
- License: GNU GPL v3 for the FFDec application.
- Integration: invoked as a separate, resource-limited subprocess; `library.swf` members from game
  SWC archives are parsed as data and are never executed.

The official release archive and its license notices are included in the container image.
Redistributors of the image must preserve those notices and satisfy the GPL source-availability
requirements. `game-downloader` records the exact FFDec version in every GameSnapshot produced from
SWC ActionScript libraries.
