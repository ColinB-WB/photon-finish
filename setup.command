#!/bin/bash
# Double-click this once in Finder to set up Photon Finish on this Mac.
DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$DIR/bin/setup.sh"
echo
read -n 1 -s -r -p "Press any key to close this window."
echo
