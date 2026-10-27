#!/usr/bin/env bash
# AI Helper (for Claude) - one-line installer for macOS and Linux. Unofficial; not affiliated with Anthropic.
#
#   curl -fsSL https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.sh | bash
#
# Running it again updates the app. Needs no admin password, no Xcode Command Line Tools, no git and no system
# Python: it downloads the source with curl, gets Python through uv (installed to ~/.local/bin if missing), makes a
# private venv and hands over to install.py (mod link, mod-list, config, Claude Code skill + MCP server, the app).
#
# Environment overrides:
#   AIHELPER_HOME     install folder (macOS: ~/Library/Application Support/ai-helper-for-claude,
#                     Linux: ~/.local/share/ai-helper-for-claude)
#   AIHELPER_SOURCE   install from a local folder or .tar.gz instead of downloading (CI, testing)
#   AIHELPER_TARBALL  download this .tar.gz URL instead (e.g. while the repository is private)
#   AIHELPER_REF      branch or tag to download (default: the latest release, else main)
#   AIHELPER_PYTHON   Python version for the venv (default 3.14)
#   AIHELPER_UV       an existing uv binary to use
# Everything after `bash -s --` is passed to install.py (e.g. --check, --uninstall).

set -euo pipefail

REPO="codysoutherland-creator/ai-helper-for-claude"
UV_VERSION="0.12.24"
# SHA-256 of https://astral.sh/uv/$UV_VERSION/install.sh (update together with UV_VERSION)
UV_INSTALLER_SHA256="e8c19f8dbfaafcbeddc8d17bf9227505d918220eba2aeb543d6922bdc5bb9625"
PY_VERSION="${AIHELPER_PYTHON:-3.14}"
# never part of an update: the user's own files in the install folder
KEEP="config.json .venv logs chat-session plans blueprints foreman-notes.md"

STAGE=""

say() { printf '%s\n' "$*" >&2; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

curl_https() {
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$@"
}

default_home() {
  case "$(uname -s)" in
    Darwin) printf '%s\n' "$HOME/Library/Application Support/ai-helper-for-claude" ;;
    Linux) printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/ai-helper-for-claude" ;;
    *) fail "unsupported system $(uname -s); on Windows use install.ps1" ;;
  esac
}

latest_ref() {
  # the latest release tag, without the API (no rate limit, no jq): /releases/latest redirects to /releases/tag/<tag>
  local url
  url="$(curl --proto '=https' --tlsv1.2 -fsSLI -o /dev/null -w '%{url_effective}' \
    "https://github.com/$REPO/releases/latest" 2>/dev/null || true)"
  case "$url" in
    */releases/tag/*) printf '%s\n' "${url##*/releases/tag/}" ;;
    *) printf 'main\n' ;;
  esac
}

fetch_source() {
  # $1 = staging folder; fills it with the app's files (top level = the repository root)
  local stage="$1" src="${AIHELPER_SOURCE:-}" ref url
  if [ -n "$src" ]; then
    if [ -d "$src" ]; then
      say "Using the local folder $src"
      # the user's own files and dev-only state never travel with a local copy
      tar -C "$src" \
        --exclude=.git --exclude=.venv --exclude=config.json --exclude=logs --exclude=chat-session \
        --exclude=plans --exclude=blueprints --exclude=testenv --exclude=foreman-notes.md \
        --exclude=dist --exclude=build --exclude=__pycache__ --exclude=.pytest_cache --exclude=MANIFEST \
        -cf - . | tar -C "$stage" -xf -
      return
    fi
    [ -f "$src" ] || fail "AIHELPER_SOURCE=$src is neither a folder nor a file"
    say "Using the archive $src"
    tar -xzf "$src" -C "$stage" --strip-components 1
    return
  fi
  if [ -n "${AIHELPER_TARBALL:-}" ]; then
    url="$AIHELPER_TARBALL"
  else
    ref="${AIHELPER_REF:-$(latest_ref)}"
    case "$ref" in
      main | master) url="https://codeload.github.com/$REPO/tar.gz/refs/heads/$ref" ;;
      *) url="https://codeload.github.com/$REPO/tar.gz/refs/tags/$ref" ;;
    esac
  fi
  case "$url" in https://*) ;; *) fail "refusing a non-HTTPS download: $url" ;; esac
  say "Downloading $url"
  curl_https "$url" | tar -xz -C "$stage" --strip-components 1 ||
    fail "download failed (is the repository public? AIHELPER_SOURCE can point at a local copy)"
}

is_kept() {
  local top="${1%%/*}" k
  for k in $KEEP; do
    [ "$top" = "$k" ] && return 0
  done
  return 1
}

update_files() {
  # copy the staged files over the install folder; delete files the previous version had and this one hasn't
  local stage="$1" home="$2" old="$2/MANIFEST" f
  [ -f "$stage/install.py" ] || fail "the download doesn't look like AI Helper (no install.py)"
  (cd "$stage" && find . -type f ! -name MANIFEST | sed 's|^\./||' | LC_ALL=C sort) >"$stage/MANIFEST"
  if [ -f "$old" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      case "$f" in /* | *..*) continue ;; esac
      is_kept "$f" && continue
      if ! grep -qxF -- "$f" "$stage/MANIFEST"; then
        rm -f -- "$home/$f"
      fi
    done <"$old"
  fi
  (cd "$stage" && tar -cf - .) | (cd "$home" && tar -xf -)
}

sha256_of() {
  # macOS ships shasum (perl) and openssl without the Command Line Tools; Linux has sha256sum
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r "$1" | cut -d' ' -f1
  else
    return 1
  fi
}

find_uv() {
  if [ -n "${AIHELPER_UV:-}" ] && [ -x "${AIHELPER_UV}" ]; then
    printf '%s\n' "$AIHELPER_UV"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    printf '%s\n' "$HOME/.local/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    command -v uv
  fi
}

ensure_uv() {
  local uv
  uv="$(find_uv)"
  if [ -z "$uv" ]; then
    say "Installing uv $UV_VERSION (Python manager) to ~/.local/bin"
    mkdir -p "$HOME/.local/bin"
    local script sum
    script="$(mktemp "${TMPDIR:-/tmp}/aihelper-uv.XXXXXX")"
    curl_https -o "$script" "https://astral.sh/uv/$UV_VERSION/install.sh" ||
      { rm -f "$script"; fail "couldn't download the uv installer"; }
    sum="$(sha256_of "$script")" || { rm -f "$script"; fail "no SHA-256 tool (shasum, sha256sum or openssl) found"; }
    if [ "$sum" != "$UV_INSTALLER_SHA256" ]; then
      rm -f "$script"
      fail "the uv $UV_VERSION installer didn't match its pinned SHA-256; not running it"
    fi
    env UV_UNMANAGED_INSTALL="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$script" >/dev/null ||
      { rm -f "$script"; fail "couldn't install uv"; }
    rm -f "$script"
    uv="$HOME/.local/bin/uv"
  fi
  [ -x "$uv" ] || fail "uv not found after installing it"
  printf '%s\n' "$uv"
}

ensure_venv() {
  # $1 = uv, $2 = install folder. Only uv-managed Python: /usr/bin/python3 on a Mac without the Command Line Tools is
  # a stub that pops up an install dialog.
  local uv="$1" home="$2" py="$2/.venv/bin/python"
  export UV_PYTHON_PREFERENCE=only-managed
  if [ -x "$py" ] && "$py" -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '$PY_VERSION' else 1)" \
    >/dev/null 2>&1; then
    return
  fi
  say "Setting up Python $PY_VERSION (private to this app)"
  "$uv" python install --no-bin "$PY_VERSION" >/dev/null
  "$uv" venv --clear --python "$PY_VERSION" "$home/.venv" >/dev/null
}

cleanup() {
  if [ -n "$STAGE" ]; then
    rm -rf "$STAGE"
  fi
}

main() {
  local home uv rc
  [ -n "${HOME:-}" ] || fail "HOME is not set"
  [ "$(id -u)" != "0" ] || fail "don't run this with sudo; it installs for your user only"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  command -v tar >/dev/null 2>&1 || fail "tar is required"

  home="${AIHELPER_HOME:-$(default_home)}"
  say "AI Helper (for Claude) installer (unofficial; not affiliated with or endorsed by Anthropic)"
  say "Install folder: $home"
  mkdir -p "$home"
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/aihelper.XXXXXX")"
  trap cleanup EXIT

  fetch_source "$STAGE"
  update_files "$STAGE" "$home"

  uv="$(ensure_uv)"
  ensure_venv "$uv" "$home"

  # install.py installs the pinned, hash-checked packages (requirements.lock) with this uv
  rc=0
  AIHELPER_UV="$uv" AIHELPER_BOOTSTRAP=1 "$home/.venv/bin/python" "$home/install.py" "$@" </dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    say ""
    say "Some steps failed (see above). Fix them and run the same command again; it's safe to repeat."
    exit "$rc"
  fi
  say ""
  say "Update or repair any time by running the same one-line command again."
}

main "$@"
