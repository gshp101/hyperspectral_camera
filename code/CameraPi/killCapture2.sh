#!/bin/sh

ssh camerapi2.local 'kill `ps -aux | grep cameraCapture | egrep -v grep | awk "{ print \\$2 }"` 2> /dev/null'
ssh camerapi2.local 'kill `ps -aux | grep captureServer | egrep -v grep | awk "{ print \\$2 }"` 2> /dev/null'
