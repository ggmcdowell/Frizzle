# Sourced by the product-repo hooks pre-commit and post-merge (not a Git hook itself). run_product_hook <hub> <hook> [args...]
# runs the repo's tracked hook as pre-push does, never by exec of the file itself: a Windows checkout has no executable
# bit and no shebang dispatch. A python shebang runs under the hub Python (a Windows python3 shebang can hit the Store
# stub), a bash one under bash, any other under sh.
run_product_hook() {
  hub=$1
  hook=$2
  shift 2
  python="$hub/.venv/bin/python"
  test -e "$python" || python="$hub/.venv/Scripts/python.exe"
  case $(head -n 1 "$hook") in
    '#!'*python*) exec "$python" -B "$hook" "$@" ;;
    '#!'*bash*) exec bash "$hook" "$@" ;;
    *) exec sh "$hook" "$@" ;;
  esac
}
