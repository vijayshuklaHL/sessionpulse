#!/bin/sh
# Build the zip for `gnome-extensions install` and extensions.gnome.org.
set -e
cd "$(dirname "$0")"
UUID=sessionpulse@vijayshuklaHL.github.io
cp ../src/sessionpulse/icons/sessionpulse-*.svg "$UUID/icons/"   # keep the icons in step with the app
mkdir -p dist
gnome-extensions pack --force --out-dir dist --extra-source=icons "$UUID"
echo "dist/$UUID.shell-extension.zip"
