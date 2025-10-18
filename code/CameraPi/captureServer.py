import tempfile

from flask import Flask, Response
import cv2
import datetime
from databaseUtilities import saveOutput, SERVER_PORT, convertRawTo12Bits
from calibration import DistortionCorrection
import os
import numpy
import argparse
import sys
import time
import tempfile
import pickle
from PIL import Image
from motor import Motor
import io

pi2WebEnvironmentVariable = 'CAMERA_CALIBRATION_PI2WEB'
singlePixelCenter, singlePixelRadius, fovBand = None, None, None
centerMax = 0
centerLoc = None
webCamOffset = [0,0]
calibrationFile = None
useOffset = True
mirroring = {
    'camerapi2': True,
    'camerapi': False,
}
doingCapture = False
captureStream = None
webStream = None
scanMotor = None
pi2WebCalibration = None
windowUpperLeft = None
windowLowerRight = None
dimensionTag = 'c'
rawDimensions = {'a': (4056, 3040), 'b':(2028,1520), 'c': (2028, 1080)}

class VideoCamera(object):
    webcam = None

    def __init__(self, idx):
        global dimensionTag
        global rawDimensions
        self.running = False
        try:
            from picamera2 import Picamera2
            from libcamera import Transform
            cam = Picamera2(idx)
        except:
            return

        self.camera = cam
        if len(cam.camera_controls) == 3:
            self.isPiCamera = False
            self.rotation = 0
            VideoCamera.webcam = self
        elif len(cam.camera_controls) >= 18:
            self.isPiCamera = True
            self.rotation = 180
        else:
            cam.close()
            raise Exception("Did not recognize the camera_controls. %s controls" % len(cam.camera_controls))
        xform = Transform(rotation=self.rotation)
        self.captureConfig = cam.create_still_configuration(raw={'format': 'SRGGB12', 'size': rawDimensions[dimensionTag]},
                                                            transform=xform)
        if self.isPiCamera:
            self.captureConfig['controls']['ExposureTime'] = 66666
        self.previewConfig = cam.create_preview_configuration(transform=xform)
        cam.start()
        cam.switch_mode(self.previewConfig)
        self.webcam = None
        self.running = True
        self.captureRaw = True

    def __del__(self):
        if self.running and self.camera.isopen:
            self.camera.stop()
            self.camera.close()

    def setWebcam(self, webcam):
        assert(isinstance(webcam, VideoCamera) and self.isPiCamera)
        self.webcam = webcam

    def setExposureTime(self, exposure):
        if exposure is not None:
            self.previewConfig['controls']['ExposureTime'] = exposure
            self.camera.switch_mode(self.previewConfig)

    def removeExposureTime(self):
        if 'ExposureTime' in self.previewConfig['controls']:
            del self.previewConfig['controls']['ExposureTime']
        self.camera.switch_mode(self.previewConfig)

    def setCaptureMode(self, flag, exposureTime = None):
        if flag:
            if isinstance(exposureTime, int) and exposureTime > 10000 and exposureTime < 10000000:
                sys.stdout.write("Setting exposure time %s for capture mode\n" % exposureTime)
                sys.stdout.flush()
            self.camera.switch_mode(self.captureConfig)
            self.camera.set_controls({'ExposureTime': exposureTime, 'AnalogueGain': 1.0})
            md = self.camera.capture_metadata('raw')
            time.sleep(1)
        else:
            if 'ExposureTime' in self.captureConfig['controls']:
                self.camera.switch_mode(self.previewConfig)

    def rawImageCapture(self):
        fname = tempfile.gettempdir() + '/raw%s-%s.dng' % (time.time(), os.getpid())
        md = self.camera.capture_file(fname, 'raw')
        with open(fname, 'rb') as fid:
            img = fid.read()
        os.remove(fname)
        return {'metadata': md, 'raw': img}

    def rawDebayerCapture(self, dims = (800, 600)):
        md = self.camera.capture_metadata('raw')
        raw = self.camera.capture_array('raw')
        return raw, md
        return numpy.array(img.resize(dims)), md
        bayer = convertRawTo12Bits(raw).astype('u2')
        img = Image.fromarray(cv2.cvtColor(bayer, cv2.COLOR_BayerRGGB2GRAY) * 16 + 15)

    def rawWindowCapture(self):
        global windowLowerRight
        global windowUpperLeft
        md = self.camera.capture_metadata('raw')
        raw = self.camera.capture_array('raw')
        md['windowed'] = False
        if windowUpperLeft is not None:
            raw = convertRawTo12Bits(raw)
            raw = raw[windowUpperLeft[1]:windowLowerRight[1], windowUpperLeft[0]:windowLowerRight[0]]
            md['windowed'] = True
        else:
            raw = convertRawTo12Bits(raw)
        return raw, md


    def jpeg(self, noEncode = False, exposureTime = None):
        global singlePixelCenter
        global pi2WebCalibration
        global fovBand
        self.setExposureTime(exposureTime)
        frame = self.camera.capture_array('main')
        if exposureTime is not None:
            self.removeExposureTime()
        frame = frame[:, :, :3][:,:,::-1]
        if noEncode: return frame
        if self.isPiCamera and pi2WebCalibration is not None and singlePixelCenter is None:
            global centerMax
            global centerLoc
            global webCamOffset
            fl = frame.flatten()
            p = numpy.argmax(fl)
            mx = fl[p]
            if True or mx > centerMax:
                row = p//(frame.shape[1]*3)
                col = (p % (frame.shape[1]*3))//3
                frame = cv2.circle(numpy.zeros(frame.shape, dtype = frame.dtype) + frame, (col, row), 5, (255,0,0), 2)
                if pi2WebCalibration.sourceDimensions is not None:
                    row *= pi2WebCalibration.sourceDimensions[0] / frame.shape[0]
                    col *= pi2WebCalibration.sourceDimensions[1] / frame.shape[1]
                centerMax = mx
                centerLoc = pi2WebCalibration(numpy.array([[row, col]])).flatten()[::-1].astype('int')
        elif not self.isPiCamera: # Webcam cursor
            global useOffset
            if fovBand is not None:
                frame = cv2.line(numpy.zeros(frame.shape, dtype = frame.dtype) + frame, [0,fovBand[0]], [frame.shape[1], fovBand[0]], (0, 0, 255), 2)
                frame = cv2.line(numpy.zeros(frame.shape, dtype = frame.dtype) + frame, [0,fovBand[1]], [frame.shape[1], fovBand[1]], (0, 0, 255), 2)
            elif singlePixelCenter is not None:
                # singlePixelRadius
                if useOffset:
                    spc = [singlePixelCenter[0]+webCamOffset[0],singlePixelCenter[1]+webCamOffset[1]]
                else:
                    spc = singlePixelCenter
                frame = cv2.circle(numpy.zeros(frame.shape, dtype = frame.dtype) + frame, spc, 5, (0, 0, 255), 2)
            elif centerLoc is not None:
                if useOffset:
                    spc = [centerLoc[0]+webCamOffset[0],centerLoc[1]+webCamOffset[1]]
                else:
                    spc = centerLoc
                frame = cv2.circle(numpy.zeros(frame.shape, dtype = frame.dtype) + frame, spc, 5, (0, 0, 255), 2)

        ret, jpeg = cv2.imencode('.jpg', frame)
        return jpeg.tobytes()

    def still(self, exposureTime = None):
        sys.stdout.write('RGB capture: %s\n'%exposureTime)
        if isinstance(exposureTime, int) and exposureTime > 60000 and exposureTime < 10000000:
            sys.stdout.write("Setting exposure time %s\n" % exposureTime)
            self.captureConfig['controls']['ExposureTime'] = exposureTime
        sys.stdout.flush()
        if self.isPiCamera:
            self.camera.switch_mode(self.captureConfig)
            time.sleep(1)
            if self.captureRaw:
                frame = self.camera.capture_array('raw')
                md = self.camera.capture_metadata('raw')
                frameRGB = None
            else:
                frame = self.camera.capture_array('main')
                frame = frame[:, :, :3][:, :, ::-1]
                frameRGB = frame
                frame = None
                md = self.camera.capture_metadata('main')
            self.camera.switch_mode(self.previewConfig)
        else:
            frame = self.camera.capture_array('main')
            frameRGB = frame[:, :, :3][:, :, ::-1]
            frame = None
            md = self.camera.capture_metadata('main')
        viewFinder = self.jpeg(noEncode=True, exposureTime=exposureTime)
        self.captureConfig['controls']['ExposureTime'] = 66666
        frame = {'frame': frame, 'frameRGB': frameRGB, 'metadata': md, 'viewFinder': viewFinder}
        if self.webcam is not None:
            frame['webcam'] = self.webcam.jpeg(noEncode = True)
        return frame

def startCameras():
    global captureStream
    global webStream
    for i in range(2):
        stream = VideoCamera(i)
        if stream.running:
            if stream.isPiCamera:
                assert(captureStream is None)
                captureStream = stream
            else:
                assert(webStream is None)
                webStream = stream

    assert(webStream is not None and captureStream is not None)
    captureStream.setWebcam(webStream)

def sendFrame(frame, oname = 'jnk.pkl'):
    fname = tempfile.gettempdir() + '/jnk.pkl'
    with open(fname, 'wb') as fid:
        pickle.dump(frame, fid)
    os.system('scp %s Mac-mini-2.local:/Users/X/Documents/Hyperspectral/CameraPi/%s' % (fname, oname))
    os.remove(fname)

def gen(camera):
    global doingCapture
    prev = camera.jpeg()
    while True:
        global doingCapture
        if doingCapture and camera.isPiCamera:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + prev + b'\r\n\r\n')
        else:
            frame = camera.jpeg()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            prev = frame

def webgen(webcam):
    while True:
        frame = webcam.jpeg()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')


def doScan(angle, fname, motor = None, exposure = None, dims = (800, 600)):
    global captureStream
    global webStream
    global doingCapture
    global windowLowerRight
    global windowUpperLeft
    if not isinstance(captureStream, VideoCamera):
        return
    info = {'angle': angle, 'hsiscan': '0.1', 'exposure': exposure}
    doingCapture = True
    sys.stdout.write('Doing scan through %f degrees and exposure %s us\n' % (angle, exposure))
    if angle > 90:
        sys.stdout.write("Scan: Can only scan less than 90 degrees\n")
        doingCapture = False
        return False
    if motor is None:
        mtr = Motor()
    else:
        mtr = motor
    if not mtr.calibrate():
        sys.stdout.write("Scan: Calibration attempt failed.\n")
        doingCapture = False
        return False
    if webStream is None:
        startCameras()
    mtr.slow()
    mtr.rotate(angle/2)
    mtr.counterClockwise()
    fid = open(fname, 'wb')
    pickle.dump(info, fid, pickle.HIGHEST_PROTOCOL)
    nframes = 0

    captureStream.setCaptureMode(True, exposure)
    overviewFrames = []
    preAngle = mtr.currentAngle
    start = time.time()
    while mtr.currentAngle > -angle/2:
        webFrame = webStream.jpeg(noEncode=True)
        frameLine = numpy.squeeze(webFrame[:,webFrame.shape[1]//2,:])
        raw, md = captureStream.rawWindowCapture()
        fid.flush()
        flag = mtr.step()
        if mtr.currentAngle < -angle / 2 or nframes == 0 or preAngle*mtr.currentAngle <= 0:
            overviewFrames.append(webFrame)
        preAngle = mtr.currentAngle
        pickle.dump({'frameLine': frameLine, 'picam': raw, 'metadata': md, 'angle': mtr.currentAngle, 'frame': nframes, 'time': time.time()-start},
                        fid, pickle.HIGHEST_PROTOCOL)
        nframes += 1

    captureStream.setCaptureMode(False, exposure)
    captureStream.removeExposureTime()
    pickle.dump({'success': flag, 'nframes': nframes, 'overviewFrames': overviewFrames, 'collectTime': time.time()-start}, fid, pickle.HIGHEST_PROTOCOL)
    fid.close()
    mtr.rotate(0)
    if motor is None:
        mtr.sleep()
    doingCapture = False
    return True

if __name__ == '__main__':
    app = Flask(__name__)

    @app.route('/')
    def index():
        return ("""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <title>Camera Pi - Hyperspectral</title>
      </head>
      <body>
      <div>
      <button hidden id="snapButton" class="button" onclick="snap()">Snap Single Photo</button>
      <button id="scanButton" class="button" onclick="scan()">Scan Photo</button>
      <button id="calibButton" class="button" onclick="calib()">Calibrate</button>
      <label for="comment">Comment:</label>
      <input type="text" id="comment" name="comment">
      <label hidden for="exposure">Exposure:</label>
      <input hidden type="text" id="exposure" name="comment" value="66666">
      <button hidden id="setOffset" class="button" onclick="set_offset()">Set Offset</button>
      <label hidden id="offsetOn">Off</label>
      </div>
      <div>
      <img src="/webcam"/>
      </div>
      <div>
      <label id="statusLabel"></label>
      </div>
      <script>
      function position(event) {
        if (document.getElementById('offsetOn').innerHTML != 'On') return;
        var rect = event.target.getBoundingClientRect();
        var x = event.clientX - rect.left; //x position within the element.
        var y = event.clientY - rect.top;  //y position within the element.
        document.getElementById('offsetOn').innerHTML = 'Off'
        if ((x < 0) || (x >=640) || (y < 0) || (y >= 480)) return
        // console.log(x + " " + y)
        var xmlHttp = new XMLHttpRequest();
        xmlHttp.open("GET", "/set_webcam_offset/" + encodeURIComponent(y) + "/" + encodeURIComponent(x), true); // true for asynchronous 
        xmlHttp.send(null);    
      }
      function clear_position() {
        document.getElementById('offsetOn').innerHTML = 'Off';
      }
      function setLabel(strng) {
        document.getElementById('statusLabel').innerHTML = strng
      }
      function snap() {
        //document.getElementById('snapButton').disabled = true;
        document.getElementById('scanButton').disabled = true;
        document.getElementById('calibButton').disabled = true;
        var comment = document.getElementById('comment').value;
        var exposure = document.getElementById('exposure').value;
        setLabel('Start Image Capture...')
        var xmlHttp = new XMLHttpRequest();
        xmlHttp.onreadystatechange = function() { 
            if (xmlHttp.readyState == 4) { 
                //document.getElementById('snapButton').disabled = false;
                document.getElementById('scanButton').disabled = false;
                document.getElementById('calibButton').disabled = false;
                if (xmlHttp.status == 200) setLabel('Image Captured: ' + xmlHttp.responseText);
                else setLabel('Image capture failed!');
            }
        }
        xmlHttp.open("GET", "/snapshot/" + encodeURIComponent(comment) + "/" + encodeURIComponent(exposure), true); // true for asynchronous 
        xmlHttp.send(null);    
      }
      function scan() {
        //document.getElementById('snapButton').disabled = true;
        document.getElementById('scanButton').disabled = true;
        document.getElementById('calibButton').disabled = true;
        var comment = document.getElementById('comment').value;
        var exposure = document.getElementById('exposure').value;
        setLabel('Start Scan Capture...')
        var xmlHttp = new XMLHttpRequest();
        xmlHttp.onreadystatechange = function() { 
            if (xmlHttp.readyState == 4) { 
                //document.getElementById('snapButton').disabled = false;
                document.getElementById('scanButton').disabled = false;
                document.getElementById('calibButton').disabled = false;
                if (xmlHttp.status == 200) setLabel('Scan completed: ' + xmlHttp.responseText);
                else setLabel('Scan failed!');
            }
        }
        xmlHttp.open("GET", "/scan/" + encodeURIComponent(comment) + "/" + encodeURIComponent(exposure), true); // true for asynchronous 
        xmlHttp.send(null);    
      }
      function calib() {
        //document.getElementById('snapButton').disabled = true;
        document.getElementById('scanButton').disabled = true;
        document.getElementById('calibButton').disabled = true;
        var comment = document.getElementById('comment').value;
        var exposure = document.getElementById('exposure').value;
        setLabel('Start Calibration...')
        var xmlHttp = new XMLHttpRequest();
        xmlHttp.onreadystatechange = function() { 
            if (xmlHttp.readyState == 4) { 
                //document.getElementById('snapButton').disabled = false;
                document.getElementById('scanButton').disabled = false;
                document.getElementById('calibButton').disabled = false;
                if (xmlHttp.status == 200) setLabel('Scan completed: ' + xmlHttp.responseText);
                else setLabel('Calibration failed!');
            }
        }
        xmlHttp.open("GET", "/calibrate", true); // true for asynchronous 
        xmlHttp.send(null);    
      }
      function set_offset() {
        document.getElementById('offsetOn').innerHTML = 'On';
      }
      </script>
      </body>
    </html>
    """)


    @app.route('/snapshot')
    @app.route('/snapshot/<exposure>')
    @app.route('/snapshot/<comment>/<exposure>')
    def snapshot(comment = 'NONE', exposure = '66666'):
        global doingCapture
        global captureStream
        doingCapture = True
        try:
            exposure = int(exposure)
        except:
            exposure = 66666
        sys.stdout.write("Using exposure %s; comment = %s\n" % (exposure, comment))
        sys.stdout.flush()
        imgName = saveOutput(captureStream.still(exposure), comment)
        doingCapture = False
        return imgName

    @app.route('/scan')
    @app.route('/scan/<exposure>')
    @app.route('/scan/<comment>/<exposure>')
    def routescan(comment = 'NONE', exposure = '66666'):
        global doingCapture
        global captureStream
        global scanMotor
        if scanMotor is None:
            sys.stdout.write("Scan motor is not present. No scan done.\n")
            sys.stdout.flush()
            return "NO SCAN"

        doingCapture = True
        try:
            exposure = int(exposure)
        except:
            exposure = 66666
        sys.stdout.write("Scanning using exposure %s; comment = %s\n" % (exposure, comment))
        sys.stdout.flush()
        tmpname = tempfile.gettempdir() + '/%s-%s.pkl' % (time.time(), os.getpid())
        if doScan(90, tmpname, motor=scanMotor, exposure=exposure):
            imgName = saveOutput(tmpname, 'SCAN: ' + comment)
            if os.path.exists(tmpname):
                sys.stdout.write("Failed to move scanfile... Deleting.\n")
                sys.stdout.flush()
                os.remove(tmpname)
        else:
            sys.stdout.write("Scan failed...\n")
            sys.stdout.flush()

        doingCapture = False
        return imgName

    @app.route('/calibrate')
    def routeCalibrate():
        global doingCapture
        global scanMotor
        sys.stdout.write("Calibrating motor position...\n")
        sys.stdout.flush()
        if scanMotor.calibrate(True):
            sys.stdout.write("Scan failed...\n")
            sys.stdout.flush()
        else:
            sys.stdout.write("Scan failed...\n")
            sys.stdout.flush()

        return "Calibrated!"

    @app.route('/video_feed')
    def video_feed():
        global captureStream
        return Response(gen(captureStream),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    @app.route('/webcam')
    def webcam():
        global webStream
        return Response(webgen(webStream),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    @app.route('/set_webcam_offset')
    @app.route('/set_webcam_offset/<row>/<col>')
    def setWebcamOffset(row = '0', col = '0'):
        global calibrationFile
        global webCamOffset
        global doingCapture
        doingCapture = False
        print("Resetting center:", row, col)
        try:
            row = int(row)
            col = int(col)
        except:
            return ('FAILED')
        if calibrationFile is not None:
            global centerLoc
            global singlePixelCenter
            global windowUpperLeft
            global windowLowerRight

            pi2WebCalibration, calibrationInfo, hsiInfo = DistortionCorrection.open(calibrationFile)

            if singlePixelCenter is not None:
                center = singlePixelCenter
            elif centerLoc is not None:
                center = centerLoc
            else:
                return ("FAILED")
            webCamOffset = [col - center[0], row - center[1]]
            calibrationInfo['webcamOffset'] = webCamOffset
            pi2WebCalibration.save(calibrationFile, calibrationInfo)
        return("OK")

    @app.route('/get_time')
    def get_time():
        tm = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return tm

    startCameras()
    scanMotor = Motor()
    if not scanMotor.calibrate():
        print("Motor calibration failed. Motor not active.")
        scanMotor = None

    parser = argparse.ArgumentParser()
    parser.add_argument('--calib', help = 'Calibration file.', default = '/home/X/Documents/Hyperspectral/pi2web_calibration.pkl')

    args = parser.parse_args()

    if os.path.exists(args.calib):
        calibrationFile = args.calib
        pi2WebCalibration, calibrationInfo, hsiInfo = DistortionCorrection.open(args.calib)
        print("Calibration with input dimension %s and degree %d" % (pi2WebCalibration.sourceDimensions, pi2WebCalibration.degree))
        if hsiInfo is not None:
            print("HSI Info section found...")
            windowUpperLeft = hsiInfo['ul'].astype('int')
            windowLowerRight = hsiInfo['lr'].astype('int')
            if not numpy.all(numpy.mod(windowUpperLeft, 2)==0) or not numpy.all(numpy.mod(windowLowerRight, 2)==0):
                print("  Windows must have even integer specification. Not true for current HSI info.")
                windowUpperLeft = None
                windowLowerRight = None
            windowUpperLeft -= 2
            windowLowerRight += 2
            if not numpy.all(windowUpperLeft >= 0) or not numpy.all(windowLowerRight < rawDimensions[dimensionTag]):
                print("  Window must pad out by two to have dimensions inside the raw image size.")
                windowLowerRight = None
                windowUpperLeft = None
            print("Loaded window with upper left: %s and lower right %s" % (windowUpperLeft, windowLowerRight))

        if 'singlePixelInformation' in calibrationInfo:
            singlePixelCenter, singlePixelRadius = calibrationInfo['singlePixelInformation']['center'], calibrationInfo['singlePixelInformation']['radius']
            if 'scan' in calibrationInfo:
                fovBand = calibrationInfo['scan']
            print(singlePixelRadius, singlePixelCenter)
        if 'webcamOffset' in calibrationInfo:
            webCamOffset = calibrationInfo['webcamOffset']
    else:
        pi2WebCalibration = None
        print("No calibration loaded.")

    app.run(host='0.0.0.0', debug=True, port=str(SERVER_PORT), use_reloader=False)
