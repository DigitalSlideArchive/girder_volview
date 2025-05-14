#!/usr/bin/env bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 67b6296064754905bf02fc3e4ba3a196faccc4b8 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall # avoid starting the build before patch-package is finished by running postinstall explicitly 
VITE_REMOTE_SERVER_URL= VITE_ENABLE_REMOTE_SAVE=true npm run build

# remove so npm publish picks up VolView/dist
rm .gitignore

cd ..