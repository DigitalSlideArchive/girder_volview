#!/bin/sh bash
rm -rf VolView
git clone https://github.com/Kitware/VolView.git

cd VolView
npm install
npm run postinstall
VUE_APP_PUBLIC_PATH=/static/built/plugins/volview npm run build

