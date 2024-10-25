#!/usr/bin/env bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 49edb19789aa920a4332655668e8957db58fb860 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
VITE_REMOTE_SERVER_URL= VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

# remove so npm publish picks up VolView/dist
rm .gitignore

cd ..