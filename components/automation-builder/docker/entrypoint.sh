#!/bin/sh
# autostation-build <package|installer|validate-nsi|shell> [args...]
#
#   package ...         -> scripts/package.py ...
#   installer ...       -> scripts/build_installer.py ...
#   validate-nsi ...    -> scripts/validate_nsi.py ...
#   shell | <anything>  -> exec the command as given (CI jobs call this image with `script:` blocks)
set -eu

B=/opt/autostation-builder/scripts

usage() {
  echo "usage: autostation-build package|installer|validate-nsi [args]" >&2
  echo "       (any other command is executed as-is)" >&2
}

cmd="${1:---help}"
case "$cmd" in
  package)       shift; exec python "$B/package.py" "$@" ;;
  installer)     shift; exec python "$B/build_installer.py" "$@" ;;
  validate-nsi)  shift; exec python "$B/validate_nsi.py" "$@" ;;
  -h|--help|help) usage; exit 0 ;;
  *)             exec "$@" ;;
esac
