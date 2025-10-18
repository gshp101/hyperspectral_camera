#!/bin/sh

ssh camerapi2.local 'kill `ps -aux | grep captureServer | egrep -v grep | awk "{ print \\$2 }"` 2> /dev/null; export DISPLAY=:0.0; /usr/bin/python3 /home/X/Documents/Hyperspectral/captureServer.py'
