#!/bin/sh bash
rm -rf VolView
git clone --branch package https://github.com/PaulHax/VolView.git

cd VolView
npm install
VUE_APP_PUBLIC_PATH=/static/built/plugins/volview npm run build

