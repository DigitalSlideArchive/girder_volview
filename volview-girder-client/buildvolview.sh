#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin d198895322665ec69649f244ee541cd7368ab66e --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

cd ..