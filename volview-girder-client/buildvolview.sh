#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin f1f7a77c5d69f411300bb6afc4475d3d0ba03eb4 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall
VUE_APP_PUBLIC_PATH=/static/built/plugins/volview VUE_APP_ENABLE_REMOTE_SAVE=true npm run build

