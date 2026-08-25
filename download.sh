#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

invocation_root="$(pwd -P)"
readonly invocation_root
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly repo_root

usage() {
  cat <<'EOF'
Download and unpack a game client into a local GameSnapshot.

Usage:
  ./download.sh TARGET [DIRECTORY] [OPTIONS]
  ./download.sh --target TARGET [DIRECTORY] [OPTIONS]

Examples:
  ./download.sh wot-eu
  ./download.sh wot-eu ./.data --language ALL
  ./download.sh mt-ru ./mt-data --language RU --client hd --workers 8

Options:
  -t, --target TARGET       Game region/server (for example, wot-eu or mt-ru).
  -l, --language LANGUAGES  One language, a comma-separated list, or ALL (default: EN).
  -c, --client TYPE         Client type: sd or hd (default: sd).
  -j, --workers NUMBER      Number of parallel workers, from 1 to 32.
  -h, --help                Show this help message.

DIRECTORY defaults to ./.data.
Completed snapshots are stored in DIRECTORY/snapshots/sha256:<identifier>.
EOF
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 2
}

target=""
destination=""
languages="EN"
client_type="sd"
workers=""
parse_options=true

while (( $# > 0 )); do
  if [[ "${parse_options}" == true ]]; then
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      --)
        parse_options=false
        shift
        continue
        ;;
      -t|--target)
        (( $# >= 2 )) || fail "$1 requires a value"
        target="$2"
        shift 2
        continue
        ;;
      --target=*)
        target="${1#*=}"
        shift
        continue
        ;;
      -l|--language|--languages)
        (( $# >= 2 )) || fail "$1 requires a value"
        languages="$2"
        shift 2
        continue
        ;;
      --language=*|--languages=*)
        languages="${1#*=}"
        shift
        continue
        ;;
      -c|--client|--client-type)
        (( $# >= 2 )) || fail "$1 requires a value"
        client_type="$2"
        shift 2
        continue
        ;;
      --client=*|--client-type=*)
        client_type="${1#*=}"
        shift
        continue
        ;;
      -j|--workers)
        (( $# >= 2 )) || fail "$1 requires a value"
        workers="$2"
        shift 2
        continue
        ;;
      --workers=*)
        workers="${1#*=}"
        shift
        continue
        ;;
      -*)
        fail "unknown option: $1"
        ;;
    esac
  fi

  if [[ -z "${target}" ]]; then
    target="$1"
  elif [[ -z "${destination}" ]]; then
    destination="$1"
  else
    fail "unexpected positional argument: $1"
  fi
  shift
done

[[ -n "${target}" ]] || {
  usage >&2
  fail "TARGET is required"
}
[[ -n "${languages//[[:space:]]/}" ]] || fail "the language list cannot be empty"
client_type="$(printf '%s' "${client_type}" | tr '[:upper:]' '[:lower:]')"
case "${client_type}" in
  sd|hd) ;;
  *) fail "client type must be sd or hd" ;;
esac
if [[ -n "${workers}" ]] &&
  { [[ ! "${workers}" =~ ^[0-9]+$ ]] || (( workers < 1 || workers > 32 )); }; then
  fail "workers must be a number from 1 to 32"
fi

destination="${destination:-.data}"
if [[ "${destination}" != /* ]]; then
  destination="${invocation_root}/${destination}"
fi
mkdir -p -- "${destination}"
destination="$(cd "${destination}" && pwd -P)"

command -v uv >/dev/null 2>&1 ||
  fail "uv was not found; install it from https://docs.astral.sh/uv/"
command -v java >/dev/null 2>&1 ||
  fail "Java was not found; install OpenJDK 17 or newer"

archive_tool=""
for candidate in 7zz 7z bsdtar; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    archive_tool="${candidate}"
    break
  fi
done
[[ -n "${archive_tool}" ]] ||
  fail "7zz, 7z, or bsdtar was not found; install 7-Zip or libarchive"

readonly ffdec_version="26.2.1"
readonly ffdec_sha256="0333b56998a55bd83f4e0deb678a811fcdc45607582b4f5dd438309c8c3ad5ce"
readonly ffdec_url="https://github.com/jindrapetrik/jpexs-decompiler/releases/download/version${ffdec_version}/ffdec_${ffdec_version}.zip"
readonly ffdec_tools_root="${repo_root}/.tools/ffdec"
readonly bundled_ffdec_root="${ffdec_tools_root}/${ffdec_version}"

ffdec_executable="${GAME_DOWNLOADER_FFDEC:-}"
if [[ -z "${ffdec_executable}" ]]; then
  ffdec_executable="${bundled_ffdec_root}/ffdec"
  if [[ ! -f "${ffdec_executable}" ]]; then
    command -v curl >/dev/null 2>&1 || fail "curl was not found; it is required to download FFDec"
    command -v unzip >/dev/null 2>&1 || fail "unzip was not found; it is required to unpack FFDec"
    mkdir -p -- "${ffdec_tools_root}"
    temporary_root="$(mktemp -d "${ffdec_tools_root}/.install-${ffdec_version}.XXXXXX")"

    cleanup_temporary_root() {
      if [[ -n "${temporary_root:-}" && -d "${temporary_root}" ]]; then
        rm -rf -- "${temporary_root}"
      fi
    }
    trap cleanup_temporary_root EXIT INT TERM

    printf 'Downloading FFDec %s...\n' "${ffdec_version}" >&2
    curl --fail --location --silent --show-error --retry 3 \
      "${ffdec_url}" --output "${temporary_root}/ffdec.zip"
    if command -v sha256sum >/dev/null 2>&1; then
      actual_ffdec_sha256="$(sha256sum "${temporary_root}/ffdec.zip" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      actual_ffdec_sha256="$(shasum -a 256 "${temporary_root}/ffdec.zip" | awk '{print $1}')"
    else
      fail "sha256sum or shasum was not found; it is required to verify FFDec"
    fi
    [[ "${actual_ffdec_sha256}" == "${ffdec_sha256}" ]] ||
      fail "FFDec checksum mismatch"

    mkdir "${temporary_root}/extracted"
    unzip -q "${temporary_root}/ffdec.zip" -d "${temporary_root}/extracted"
    [[ -f "${temporary_root}/extracted/ffdec" ]] ||
      fail "the FFDec archive does not contain the expected executable"
    chmod 0555 \
      "${temporary_root}/extracted/ffdec" \
      "${temporary_root}/extracted/ffdec.sh"
    if [[ -e "${bundled_ffdec_root}" ]]; then
      fail "the FFDec directory appeared during installation; run the command again"
    fi
    mv "${temporary_root}/extracted" "${bundled_ffdec_root}"
    temporary_root=""
    trap - EXIT INT TERM
  fi
elif [[ "${ffdec_executable}" != /* ]]; then
  ffdec_executable="${invocation_root}/${ffdec_executable}"
fi

[[ -x "${ffdec_executable}" ]] ||
  fail "FFDec was not found or is not executable: ${ffdec_executable}"
set +e
ffdec_help="$("${ffdec_executable}" -help 2>&1)"
ffdec_status=$?
set -e
if (( ffdec_status != 0 )) ||
  [[ "${ffdec_help}" != *"JPEXS Free Flash Decompiler v.${ffdec_version}"* ]]; then
  fail "FFDec failed the ${ffdec_version} version check; verify Java and GAME_DOWNLOADER_FFDEC"
fi
export GAME_DOWNLOADER_FFDEC="${ffdec_executable}"

cd "${repo_root}"
download_command=(
  uv run --frozen game-downloader run
  --target "${target}"
  --client-type "${client_type}"
  --languages "${languages}"
  --data-root "${destination}"
)
if [[ -n "${workers}" ]]; then
  download_command+=(--download-workers "${workers}")
fi

printf 'Target: %s | client: %s | languages: %s\n' \
  "${target}" "${client_type}" "${languages}" >&2
printf 'Local directory: %s\n\n' "${destination}" >&2
"${download_command[@]}"

printf '\nDone. Verified snapshots: %s/snapshots\n' "${destination}"
