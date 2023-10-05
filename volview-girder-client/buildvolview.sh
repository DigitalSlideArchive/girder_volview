#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin a25452bb36912a14e1f02e43bc24673923954783 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
 VITE_REMOTE_SERVER_URL= VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

# remote so npm publish picks up VolView/dist
rm .gitignore

cd ..