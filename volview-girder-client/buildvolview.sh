#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 1c33fae4a7ac4861c716f755eece6b6fdd70520f --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package done by running postinstall manualy 
 VITE_REMOTE_SERVER_URL= VITE_ENABLE_REMOTE_SAVE=true npm run build -- --base=/static/built/plugins/volview

# remote so npm publish picks up VolView/dist
rm .gitignore

cd ..