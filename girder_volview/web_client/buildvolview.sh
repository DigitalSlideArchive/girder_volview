#!/bin/sh bash
rm -rf VolView
mkdir VolView
cd VolView

# fetch just one commit
git init
git remote add origin https://github.com/Kitware/VolView.git
git fetch origin 44b05941bb85c77bf8fe5b037f7a9a4e8ce67ff2 --depth 1
git reset --hard FETCH_HEAD

npm install
npm run postinstall
VUE_APP_PUBLIC_PATH=/static/built/plugins/volview VUE_APP_ENABLE_REMOTE_SAVE=true npm run build

