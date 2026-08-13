#!/usr/bin/env bash
set -euo pipefail

readonly tippecanoe_version='v2.79.0'
readonly tippecanoe_tag='2.79.0'
readonly tippecanoe_commit='68ab8dcc229f95b8b25877697d5e8d66783af503'

# The progress profile needs GDAL as soon as its first released COG appears.
# The remaining packages are the Linux prerequisites in Tippecanoe's guide.
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  g++ gdal-bin make libsqlite3-dev zlib1g-dev
gdalinfo --version

if command -v tippecanoe >/dev/null 2>&1 &&
   [[ "$(tippecanoe --version 2>&1)" == "tippecanoe ${tippecanoe_version}" ]]; then
  exit 0
fi

tippecanoe_build_root="$(mktemp -d)"
trap 'rm -rf "${tippecanoe_build_root}"' EXIT
tippecanoe_source="${tippecanoe_build_root}/tippecanoe"

git clone --quiet --depth 1 --branch "${tippecanoe_tag}" \
  https://github.com/felt/tippecanoe.git "${tippecanoe_source}"
observed_commit="$(git -C "${tippecanoe_source}" rev-parse HEAD)"
if [[ "${observed_commit}" != "${tippecanoe_commit}" ]]; then
  echo "Tippecanoe tag resolved to ${observed_commit}, expected ${tippecanoe_commit}" >&2
  exit 1
fi

make -C "${tippecanoe_source}" -j2 tippecanoe
sudo install -m 0755 "${tippecanoe_source}/tippecanoe" /usr/local/bin/tippecanoe

observed_version="$(tippecanoe --version 2>&1)"
if [[ "${observed_version}" != "tippecanoe ${tippecanoe_version}" ]]; then
  echo "Unexpected Tippecanoe version: ${observed_version}" >&2
  exit 1
fi
