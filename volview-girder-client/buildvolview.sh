#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 2253666a560da6e42f20cea89659a48cc924cc99 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

cd ..