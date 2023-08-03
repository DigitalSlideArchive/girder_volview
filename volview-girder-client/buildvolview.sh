#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 757a25b5f69ebf5c7bf335765f97a2474cf57496 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

cd ..