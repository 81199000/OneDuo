# 被 source：设置 CARGO_ENCODED_RUSTFLAGS，让 Rust 编译产物里的路径不带本机用户目录。
# 顺序有讲究：rustc 取「最后一个匹配」的映射，所以越具体的路径放越后面。
_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
_cargo="${CARGO_HOME:-$HOME/.cargo}"
_sep=$'\x1f'
CARGO_ENCODED_RUSTFLAGS="--remap-path-prefix=$HOME=/home${_sep}--remap-path-prefix=$_cargo=/cargo${_sep}--remap-path-prefix=$_root=/src"
# 覆盖了 config.toml 里的 rustflags，所以 Mac 的最低系统版本参数要一起带上（Windows 目标不需要，但带上也无害——只对 darwin 链接器生效）
case "${ONETAP_RUST_TARGET:-mac}" in
  mac) CARGO_ENCODED_RUSTFLAGS="$CARGO_ENCODED_RUSTFLAGS${_sep}-C${_sep}link-arg=-mmacosx-version-min=10.13" ;;
esac
export CARGO_ENCODED_RUSTFLAGS
unset _root _cargo _sep
