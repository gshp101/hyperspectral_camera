# I use lot's of libraries including PyQt, PIL, h5py
from PyQt6 import QtGui       # extends QtCore with GUI functionality
from PyQt6 import QtCore       # extends QtCore with GUI functionality
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import QLabel, QApplication, QMessageBox, QMainWindow, QToolBar, QFileDialog, QComboBox, QGridLayout, QWidget, QVBoxLayout, QLabel, QGroupBox, QPushButton, QSlider, QCheckBox, QHBoxLayout
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
import OpenGL.GL as gl
from PIL import Image
import hdf5storage
import argparse

import os
import sys
# This path id not included it's where I get the plot function
# You'll have to write your own if you want spectrum plotting to work.
sys.path.append('visualization/src')
from visualization import plot
sys.path.append('CameraPi')
from databaseUtilities import getFilesById, DataLook
import numpy
import pickle
import collections
import typing

class ImageWindow(QOpenGLWidget):
    # Tag that must be in a valid annotation dictionary
    AnnotationTag = 'annotation_version'

    # Colors for annotations
    ClassMap = [
        [1., 0., 0., 1.0],
        [0., 1., 0., 1.0],
        [0., 0., 1., 1.0],
        [1., 1., 0., 1.0],
        [1., 0., 1., 1.0],
        [0., 1., 1., 1.0],
        [1., 1., 1., 1.0]
    ]

    ClassNames = [
        'Red',
        'Green',
        'Blue',
        'Yellow',
        'Magenta',
        'Turquoise',
        'White'
    ]

    def __init__(self, systemSignals, identifier = None):
        QWidget.__init__(self)
        # This dictionary is a way for the main program to control view properties
        self.systemSignals = systemSignals
        # Identification foor this window may be used eventually for multiple windows
        self.identifier = identifier
        # Clear out the data store
        self.textureId = None
        self.clearAll()
        # Width and height of view window
        self.width = 0
        self.height = 0

    def clearAll(self):
        # Texture ID of the image being displayed
        if self.textureId is not None:
            gl.glDeleteTextures(1, self.textureId)
        self.textureId = None
        # Flag to indicate if painting should be done
        self.loaded = False
        # Location of the camera
        self.camera = [0,0,0]
        # Zoom scale
        self.scale = 1.0
        # Data that will be displayed
        self.displayTexture = None
        # Array that defines  the viewport in image pixels
        self.viewport = None
        # Annotations that are displayed
        self.annotations = { 'points': [], 'rects': [], self.AnnotationTag: "1.0" }
        self.annotationModified = False
        # A reatangle that defines a selection area
        self.selectionRect = None
        # Color of selection rectangle (changes to indicate selection going on)
        self.selectionColorIndex = 0
        # Default contrast/bias
        self.contrast = 45
        self.bias = 0

    def clearAnnotations(self):
        self.annotations = {'points': [], 'rects': [], self.AnnotationTag: "1.0"}

    def loadAnnotations(self, fname):
        if not self.loaded:
            return 'No image file is loaded.'
        try:
            with open(fname, 'rb') as fid:
                data = pickle.load(fid)
        except:
            return 'Could not load input file %s as a pickle file.' % fname
        if not isinstance(data,dict) or not set(self.annotations.keys()).issubset(set(data.keys())):
            return 'Input file %s does not have the right data structure.' % fname
        self.annotations = data
        self.update()
        return ''

    def saveAnnotations(self, fname):
        if not self.loaded:
            return ''
        try:
            with open(fname, 'wb') as fid:
                pickle.dump(self.annotations, fid)
            return ''
        except:
            return 'Could not save to file %s.' % fname

    def distanceSquaredToSegment(self, pt1, pt2, pos):
        """Compute the distance of a point at pos to a line segment pt1-pt2"""
        pt1 = numpy.array(pt1)
        pt2 = numpy.array(pt2)
        delta1 = pt2 - pt1
        delta0 = pt1 - pos
        # Minimize |(pt2-pt1)*t + pt1 - pos|^2 for t in [0,1]
        A = (delta1**2).sum()
        # If the segment is degenrate
        if A == 0:
            return (delta0*delta0).sum()
        B = (delta1*delta0).sum()
        C = (delta0**2).sum()
        t = -B/A
        t = max([0, min([1, t])])
        return A*t*t + 2*B*t + C

    def distanceSquaredToRect(self, rect, pt):
        """Compute the distance from a point to a rectangle"""
        pt1, pt2, _ = rect
        # Get the shortest distance to a side
        arr = [self.distanceSquaredToSegment([pt1[0], pt1[1]], [pt1[0], pt2[1]], pt),
               self.distanceSquaredToSegment([pt2[0], pt2[1]], [pt1[0], pt2[1]], pt),
               self.distanceSquaredToSegment([pt2[0], pt2[1]], [pt2[0], pt1[1]], pt),
               self.distanceSquaredToSegment([pt1[0], pt1[1]], [pt2[0], pt1[1]], pt)]
        return min(arr)

    def setViewport(self, fromView = None):
        """Compute the viewport for a given image"""
        if not self.loaded:
            return
        # If another display is given copy that viewport
        if fromView is not None:
            self.camera = fromView.Camera
            self.viewport = fromView.viewport
            return
        # Compute the viewport based on the aspects of the window view and the image
        imageAspect = self.displayTexture.shape[1] / self.displayTexture.shape[0]
        if self.height != 0:
            viewAspect = self.width / self.height
        else:
            viewAspect = 1
            self.height = 1000
            self.width = 1000
        byWidth = ((self.displayTexture.shape[0] > self.displayTexture.shape[1] and viewAspect < imageAspect) or
                   (self.displayTexture.shape[0] <= self.displayTexture.shape[1] and viewAspect <= imageAspect))
        if byWidth:
            screenDistance = self.height*self.displayTexture.shape[1]/self.width
            assert(screenDistance > self.displayTexture.shape[0])
            padding = (screenDistance - self.displayTexture.shape[0])/2
            viewport = [0, self.displayTexture.shape[1], self.displayTexture.shape[0] + padding, -padding]
        else:
            screenDistance = self.width*self.displayTexture.shape[0]/self.height
            assert(screenDistance > self.displayTexture.shape[1])
            padding = (screenDistance - self.displayTexture.shape[1])/2
            viewport = [-padding, padding + self.displayTexture.shape[1], self.displayTexture.shape[0], 0]

        self.camera[0] = max([0, min([self.displayTexture.shape[1], self.camera[0]])])
        self.camera[1] = max([0, min([self.displayTexture.shape[0], self.camera[1]])])
        recenter = numpy.array([viewport[0]-self.camera[0], viewport[1]-self.camera[0], viewport[2]-self.camera[1], viewport[3]-self.camera[1]])/self.scale
        viewport = [recenter[0]+self.camera[0], recenter[1]+self.camera[0], recenter[2]+self.camera[1], recenter[3]+self.camera[1]]
        # viewport is arguments to glOrtho
        self.viewport = viewport + [-1, 1]

    def convertScreenToPixel(self, event):
        """Find the pixel of a given event position"""
        if self.viewport is None:
            return None
        fracX = event.position().x()/self.width
        fracY = event.position().y()/self.height
        x = fracX*abs(self.viewport[0]-self.viewport[1]) + self.viewport[0]
        y = fracY*abs(self.viewport[2]-self.viewport[3]) + self.viewport[3]
        return(numpy.array([x,y]))

    def mousePressEvent(self, event):
        pos = self.convertScreenToPixel(event)
        if pos is None: return
        if self.systemSignals['mode'] == 'point':
            pos = numpy.round(pos)
            if event.button().value == 1:
                self.annotations['points'].append(tuple(pos) + (self.systemSignals['class'],))
                self.annotationModified = True
            elif event.button().value == 2:
                self.annotations['points'] = [pt for pt in self.annotations['points'] if ((pt[0]-pos[0])**2+(pt[1]-pos[1])**2) > 9]
                self.annotationModified = True
            self.update()
        elif self.systemSignals['mode'] == 'rect':
            # Remove any rectangles close to click
            if event.button().value == 2:
                self.annotations['rects'] = [rect for rect in self.annotations['rects'] if self.distanceSquaredToRect(rect, pos) > 9]
                self.annotationModified = True
            else:
                self.lastPos = pos
        else:
            self.lastPos = pos

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        currentPos = self.convertScreenToPixel(event)
        if currentPos is None: return
        if self.systemSignals['mode'] == '':
            delta = (currentPos - self.lastPos)*1.2
            self.lastPos = currentPos
            self.camera[0] -= delta[0]
            self.camera[1] -= delta[1]
            self.setViewport()
        elif self.systemSignals['mode'] == 'rect':
            self.selectionRect = (self.lastPos, currentPos)
        self.update()

    def mouseReleaseEvent(self, event):
        if (self.systemSignals['mode'] == 'rect' and self.selectionRect is not None and event.button().value == 1 and
            numpy.linalg.norm(self.selectionRect[0]-self.selectionRect[1]) > 5):
            self.annotations['rects'].append(self.selectionRect + (self.systemSignals['class'],))
            self.annotationModified = True
        self.selectionRect = None
        self.update()

    def wheelEvent(self, event):
        currentCenter = self.convertScreenToPixel(event)
        if currentCenter is None:
            return
        scaleChange = event.angleDelta().y()/100.
        self.scale *= 1 + scaleChange
        self.scale = max([1.0, min([self.scale, 100])])
        scaleChange = max([0.0, min([abs(scaleChange), 1.0])])
        self.camera[0] = (1-scaleChange)*self.camera[0]+scaleChange*currentCenter[0]
        self.camera[1] = (1-scaleChange)*self.camera[1]+scaleChange*currentCenter[1]
        self.setViewport()
        self.update()

    def setImage(self, img = None, scaleMin = None, scaleMax = None, resetViewport = False):
        """Set the display image"""
        if self.camera is None:
            return
        # If no image is given and an image exists, we will reset the view
        if img is None:
            if self.displayTexture is not None:
                img = self.displayTexture
            else: return
        # Get either 1 band or 3 band image data
        img = numpy.squeeze(img)
        if len(img.shape) == 3 and img.shape[2] > 3:
            img  = img[:,:,:3]
        # Convert to uint8 by scaling
        if img.dtype != 'uint8':
            mn = img.min() if scaleMin is None else scaleMin
            mx = img.max() if scaleMax is None else scaleMax
            if mn == mx: mx = mn + 1
            img = (255*(img.astype('double') -mn)/(mx-mn)).astype('uint8')
        # Make sure the dimensions work
        assert(len(img.shape) == 2 or (len(img.shape)==3 and img.shape[2] == 3))
        # Copy off the data
        self.displayTexture = img.copy()
        self.camera = [self.displayTexture.shape[1]/2,self.displayTexture.shape[0]/2,0]
        self.scale = 1.0
        self.loaded = True
        if resetViewport or self.viewport is None:
            self.setViewport()
        # view is left, right, bottom, top
        self.setContrastBias()

    def initializeGL(self):
        """Basic OpenGL initialization"""
        gl.glClearColor(0.5, 0.5, 0.5, 1)  # initialize the screen to black
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glPointSize(10)
        gl.glLineWidth(3)

    def resizeGL(self, width, height):
        """Resize callback"""
        gl.glViewport(0, 0, width, height)
        self.width = width
        self.height = height
        self.setViewport()

    def drawRect(self, pt1, pt2, cls):
        """Draw a rectangle"""
        gl.glBegin(gl.GL_LINES)
        gl.glColor4f(*self.ClassMap[cls])
        gl.glVertex3f(pt1[0], pt1[1], 0.8)
        gl.glVertex3f(pt1[0], pt2[1], 0.8)
        gl.glVertex3f(pt1[0], pt2[1], 0.8)
        gl.glVertex3f(pt2[0], pt2[1], 0.8)
        gl.glVertex3f(pt2[0], pt2[1], 0.8)
        gl.glVertex3f(pt2[0], pt1[1], 0.8)
        gl.glVertex3f(pt2[0], pt1[1], 0.8)
        gl.glVertex3f(pt1[0], pt1[1], 0.8)
        gl.glEnd()

    def drawAnnotations(self):
        # Disable texture drawing (screws up the color)
        gl.glDisable(gl.GL_TEXTURE_2D)

        # Draw click points
        gl.glBegin(gl.GL_POINTS)
        for x, y, cls in self.annotations['points']:
            gl.glColor4f(*self.ClassMap[cls])
            gl.glVertex3f(x, y, 0.9)
        gl.glEnd()

        # Draw rectangles
        for pt1, pt2, cls in self.annotations['rects']:
            self.drawRect(pt1, pt2, cls)

        # Draw selection rectangle if selection is going on
        if self.selectionRect is not None:
            pt1, pt2 = self.selectionRect
            self.drawRect(pt1, pt2, self.selectionColorIndex)
        self.selectionColorIndex = (self.selectionColorIndex + 1) % len(self.ClassMap)
        gl.glColor3f(1, 1, 1)

    def paintGL(self):
        if not self.loaded:
            return
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(*self.viewport)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        if not self.loaded: return
        gl.glPushMatrix()
        gl.glLoadIdentity()
        gl.glDisable(gl.GL_LIGHTING)
        self.drawAnnotations()
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.textureId)
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2i(0, 0), gl.glVertex3i(0, 0, 0)
        gl.glTexCoord2i(0, 1), gl.glVertex3i(0, self.displayTexture.shape[0], 0)
        gl.glTexCoord2i(1, 1), gl.glVertex3i(self.displayTexture.shape[1], self.displayTexture.shape[0], 0)
        gl.glTexCoord2i(1, 0), gl.glVertex3i(self.displayTexture.shape[1], 0, 0)
        gl.glEnd()
        gl.glColor4f(1.0, 1.0, 1.0, 1.0);
        gl.glPopMatrix()

    def setContrastBias(self, contrast = None, bias = None):
        if self.displayTexture is None:
            return
        if contrast is None:
            contrast = self.contrast
        if bias is None:
            bias = self.bias

        self.contrast = contrast
        self.bias = bias

        contrast = numpy.tan(contrast*numpy.pi/180)
        img = numpy.clip(bias + self.displayTexture.astype('double')*contrast, 0, 255).astype('uint8')
        if self.textureId is not None:
            gl.glDeleteTextures(1, self.textureId)
        # Set up OpenGL to display the texture
        self.textureId = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.textureId)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        # Display as either 1 or 3 band
        if len(img.shape) == 2:
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_LUMINANCE,
                            img.shape[1], img.shape[0],
                            0, gl.GL_LUMINANCE, gl.GL_UNSIGNED_BYTE, img.flatten())
        else:
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB,
                            img.shape[1], img.shape[0],
                            0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE, img.flatten())
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        self.update()

class BandManager(object):
    def __init__(self, parent):
        self.bandMenus = []
        self.bandRequestors = []
        self.parent = parent
        self.changeDisabled = False

    NoBandName = 'None'
    AllBandName = 'All'
    class BandSelector(QComboBox):
        def __init__(self, parent, manager, threeBandOnly):
            QComboBox.__init__(self, parent)
            self.manager = manager
            self.parent = parent
            self.addItem(manager.NoBandName)
            if not threeBandOnly:
                self.addItem('Red')
                self.addItem('Green')
                self.addItem('Blue')
            self.addItem(manager.AllBandName)
            self.threeBandOnly = threeBandOnly
            self.currentIndexChanged.connect(lambda idx, mgr = self.manager, slf = self: mgr.itemChanged(idx, slf))

    def itemChanged(self, idx, comboBox):
        if self.changeDisabled: return
        self.changeDisabled = True
        enabled = False
        for j, menu in enumerate(self.bandMenus):
            enabled = enabled or menu.isEnabled()
            if menu == comboBox:
                currentText, currentCount = comboBox.currentText(), comboBox.count()
                break
        if currentText != self.NoBandName:
            for j, menu in enumerate(self.bandMenus):
                if menu != comboBox and (currentText == self.AllBandName or currentText == menu.currentText() or menu.currentText() == self.AllBandName):
                    menu.setCurrentIndex(0)
        else:
            found = False
            for j, menu in enumerate(self.bandMenus):
                if menu.currentText() != self.NoBandName:
                    found = True
                    break
            if not found:
                self.bandMenus[0].setCurrentText(self.AllBandName)
        self.changeDisabled = False
        if enabled: self.parent.updateImage()

    def setBaseState(self, idx = -1, noView = False):
        if len(self.bandMenus) == 0:
            return
        if not noView:
            self.itemChanged(0, self.bandMenus[idx])

    def getBandMenu(self, requestor, threeBandOnly = False):
        combo = self.BandSelector(self.parent, self, threeBandOnly)
        self.bandMenus.append(combo)
        self.bandRequestors.append(requestor)
        return combo

    def getDisplayImage(self):
        bands = {}
        currentShape = None
        for menu, requestor in zip(self.bandMenus, self.bandRequestors):
            if menu.currentText() != self.NoBandName:
                currentBand = requestor()
                bands[menu.currentText()] = currentBand
                if currentShape is None:
                    currentShape = currentBand.shape
                else:
                    assert(currentShape == currentBand.shape)
        if len(bands) == 1 and list(bands.keys())[0] == self.AllBandName:
            return bands[self.AllBandName]
        result = numpy.zeros(currentShape+ (3,), dtype = 'double')
        for idx, color in enumerate(['Red', 'Green', 'Blue']):
            if color in bands:
                result[:,:,idx] = bands[color]
        return result

class OperationLayout(QVBoxLayout):

    NoFileLabel = 'None'
    def __init__(self, threeChannels, parent):
        QVBoxLayout.__init__(self)
        self.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMaximumSize)
        self.threeChannel = threeChannels
        self.projection = None
        self.referenceImage = None
        self.parent = parent
        if threeChannels:
            btn = QPushButton("Load Image")
            btn.clicked.connect(self.loadOverviewFile)
            self.addWidget(btn)

            reverseRGB = QPushButton("Reverse RGB")
            reverseRGB.clicked.connect(self.reverseReferenceRGB)
            self.addWidget(reverseRGB)

            self.label = QLabel(self.NoFileLabel)
            self.addWidget(self.label)
            self.button = btn
            self.slider = None
            self.sliderCheck = None
            self.combo = parent.bandManager.getBandMenu(self.getBand, threeChannels)
        else:
            useSlider = QCheckBox("Use Band Slider")
            useSlider.setChecked(True)
            useSlider.setEnabled(False)
            useSlider.stateChanged.connect(self.setReferenceFromCube)
            self.sliderCheck = useSlider
            self.addWidget(useSlider)
            slder = QSlider()
            slder.setOrientation(QtCore.Qt.Orientation.Horizontal)
            slder.setMinimum(0)
            slder.setMaximum(1)
            self.slider = slder
            slder.valueChanged.connect(self.setReferenceFromCube)
            self.addWidget(slder)
            btn = QPushButton("Load Filter")
            btn.clicked.connect(self.loadFilterFile)
            self.button = btn
            self.addWidget(btn)
            self.label = QLabel(self.NoFileLabel)
            self.addWidget(self.label)
            self.combo = parent.bandManager.getBandMenu(self.getBand, threeChannels)

        self.addWidget(self.combo)
        self.setEnable(False)

    def maximumSize(self):
        return QtCore.QSize(200, 300)

    def reverseReferenceRGB(self):
        if self.referenceImage is not None:
            self.referenceImage = self.referenceImage[:,:,::-1]
        self.parent.updateImage()

    def setReferenceFromCube(self):
        if self.sliderCheck is not None and self.sliderCheck.isChecked():
            self.slider.setEnabled(True)
            value = self.slider.value()
            self.referenceImage = self.parent.cube[:,:,value]
            if self.parent.wavelengths is not None:
                tip = '%d, %0.0f' % (value, self.parent.wavelengths[value])
                wl = 'Index: %d; Wavelength: %d' % (value, int(self.parent.wavelengths[value]+0.5))
            else:
                tip = str(value)
                wl = "Index: %d" % value
            self.slider.setToolTip(tip)
            self.parent.wavelengthLabel.setText(wl)
        elif self.slider is not None:
            self.slider.setEnabled(False)
            if self.projection is None:
                self.sliderCheck.setChecked(True)
                return

            if isinstance(self.parent.cube, numpy.ndarray):
                data = self.parent.cube.reshape((-1,self.projection.shape[0])).dot(self.projection)

                if len(data.shape) > 1:
                    data = numpy.sqrt((data*data).sum(axis=1)/self.parent.cubeNorm)
                self.referenceImage = data.reshape(self.parent.cube.shape[:2])
        else:
            return
        self.parent.updateImage()

    def loadFilterFile(self):
        fname, _ = QFileDialog.getOpenFileName(
            self.parent, "Open Filter File", os.curdir, "Filter File (*.pkl)",
            options = QFileDialog.Option.DontUseNativeDialog)
        if not fname:
            return
        with open(fname, 'rb') as fid:
            data = pickle.load(fid)
        if isinstance(data, numpy.ndarray) and ((len(data.shape) == 2 and data.shape[0] == data.shape[1]) or len(data.shape) == 1) and data.shape[0] == self.parent.cube.shape[2]:
            self.label.setText(fname.split('/')[-1])
            self.projection = data
            self.sliderCheck.setChecked(False)
            self.setReferenceFromCube()
        else:
            QMessageBox.warning(self.parent, 'Bad Projection File', "Filter file is not appropriate for this cube.")

    def setOverviewImage(self, img, fname, noupdate = False):
        if img is None or img.shape[:2] != self.parent.cube.shape[:2]:
            return
        if len(img.shape) == 2 or (len(img.shape) == 3 and img.shape[2] == 3):
            self.referenceImage = img
            self.label.setText(fname.split('/')[-1])
            self.setEnable(True)
            if not noupdate: self.parent.updateImage()

    def loadOverviewFile(self, unused = None, refn = None):
        if refn is None:
            fname, _ = QFileDialog.getOpenFileName(
                self.parent, "Open Reference Image", os.curdir, "Image Files (*.jpg *.png)",
                options = QFileDialog.Option.DontUseNativeDialog)
            if not fname:
                return
        elif isinstance(refn, str):
            fname = refn
        else:
            return
        img = numpy.squeeze(numpy.array(Image.open(fname)))
        self.setOverviewImage(img, fname)

    def getBand(self):
        if self.referenceImage is None:
            self.setReferenceFromCube()
        return self.referenceImage

    def setEnable(self, flag):
        if not flag or not self.combo.threeBandOnly or self.label.text() != self.NoFileLabel: self.combo.setEnabled(flag)
        if self.sliderCheck: self.sliderCheck.setEnabled(flag)
        if self.slider: self.slider.setEnabled(flag)
        self.button.setEnabled(flag)

class ControlLayout(QVBoxLayout):
    def __init__(self, parent):
        self.parent = parent
        QVBoxLayout.__init__(self)
        s1 = QGridLayout()
        label = QLabel("Contrast")
        slider = QSlider()
        slider.setOrientation(QtCore.Qt.Orientation.Horizontal)
        slider.setMinimum(0)
        slider.setMaximum(89)
        slider.setValue(45)
        slider.valueChanged.connect(self.setImageProperties)
        self.contrast = slider
        s1.addWidget(label, 0, 0, 1, 5)
        s1.addWidget(slider, 0, 2, 1, 5)
        self.addLayout(s1)
        s2 = QGridLayout()
        label = QLabel("Bias")
        slider = QSlider()
        slider.setOrientation(QtCore.Qt.Orientation.Horizontal)
        slider.setMinimum(-128)
        slider.setMaximum(127)
        slider.setValue(0)
        slider.valueChanged.connect(self.setImageProperties)
        self.bias = slider
        s2.addWidget(label, 0, 0, 1, 5)
        s2.addWidget(slider, 0, 2, 1, 5)
        self.addLayout(s2)
        self.addStretch(-1)

    def maximumSize(self):
        return QtCore.QSize(200, 300)

    def setImageProperties(self):
        contrast = self.contrast.value()
        bias = self.bias.value()
        self.parent.display.setContrastBias(contrast, bias)

    def reset(self):
        self.contrast.setValue(45)
        self.bias.setValue(0)

class MainWindow(QMainWindow):

    def loadFromArgs(self):
        if self.args.file is not None:
            try:
                fidx = int(self.args.file)
            except:
                fidx = None
                fname = self.args.file
            if fidx is not None:
                arr = getFilesById(fidx)
                assert (len(arr) == 1)
                fname = arr[0]
            print(fname)
            self.load(filename=fname)

    def __init__(self, args):
        QMainWindow.__init__(self)
        self.args = args
        self.displaySignals = {'mode': '', 'class': 0}
        self.bandManager = BandManager(self)

        grid = QGridLayout()
        gridControl = QVBoxLayout()
        grid.addLayout(gridControl, 0, 0, 1, 1)
        self.display = ImageWindow(self.displaySignals)
        grid.addWidget(self.display, 0, 1, 1, 5)
        self.sliders = []
        self.cube = None
        self.cubeNorm = None

        self.operationControls = []
        def ChannelWidget(threeChannels, label, self):
            outer = QGroupBox(label)
            layout = OperationLayout(threeChannels, self)
            self.operationControls.append(layout)
            outer.setLayout(layout)
            return outer

        arr = [
            ChannelWidget(False, "HS Band 1", self),
            ChannelWidget(False, "HS Band 2", self),
            ChannelWidget(False, "HS Band 3", self),
            ChannelWidget(True, "Reference Image", self)
        ]
        for w in arr:
            gridControl.addWidget(w)

        self.controlLayout = ControlLayout(self)
        gridControl.addLayout(self.controlLayout)
        gridControl.addStretch(-1)
        # self.bandManager.setBaseState()

        toolBar = QToolBar()
        self.addToolBar(toolBar)
        fileMenu = self.menuBar().addMenu("&File")
        loadAction = QtGui.QAction("&Load...", self, shortcut="Ctrl+L", triggered=self.load)
        loadAnnotationAction = QtGui.QAction("&Load Annotation...", self, shortcut="Ctrl+K", triggered=self.loadAnnotation)
        saveAnnotationAction = QtGui.QAction("&Save Annotation...", self, shortcut="Ctrl+S", triggered=self.saveAnnotation)
        saveAnnotationSpectraAction = QtGui.QAction("&Save Annotation Spectra...", self, shortcut="Ctrl+D", triggered=self.saveAnnotationSpectra)
        toolBar.addAction(loadAction)
        wavelengthLabel = QLabel("Wavelength: --")
        toolBar.addWidget(wavelengthLabel)
        self.wavelengthLabel = wavelengthLabel
        exitAction = QtGui.QAction("E&xit", self, shortcut="Ctrl+Q", triggered=self.close)
        fileMenu.addAction(loadAction)
        fileMenu.addAction(loadAnnotationAction)
        fileMenu.addAction(saveAnnotationAction)
        fileMenu.addAction(saveAnnotationSpectraAction)
        fileMenu.addAction(exitAction)
        modeMenu = self.menuBar().addMenu("&Mode")
        modeAction = QtGui.QAction("Navigate", self, triggered=lambda: self.setSignal('mode', ''))
        modeMenu.addAction(modeAction)
        modeAction = QtGui.QAction("Points", self, triggered=lambda: self.setSignal('mode', 'point'))
        modeMenu.addAction(modeAction)
        modeAction = QtGui.QAction("Rectangles", self, triggered=lambda: self.setSignal('mode', 'rect'))
        modeMenu.addAction(modeAction)
        modeMenu.addSeparator()
        modeAction = QtGui.QAction("Clear Annotations", self, triggered=self.clearAnnotations)
        modeMenu.addAction(modeAction)
        plotMenu = self.menuBar().addMenu("&Plot")
        plotAction = QtGui.QAction("Plot Points", self, triggered=self.plotPointSpectra)
        rectAction = QtGui.QAction("Plot Rects", self, triggered=self.plotRectSpectra)
        plotMenu.addAction(plotAction)
        plotMenu.addAction(rectAction)
        classMenu = self.menuBar().addMenu("&Class")
        for i in range(7):
            classAction = QtGui.QAction("Class %d" % i, self, triggered=lambda unused=None, cl=i: self.setSignal('class', cl))
            classMenu.addAction(classAction)

        aboutMenu = self.menuBar().addMenu("About")
        aboutAct = QtGui.QAction("About Display", self, triggered=self.about)
        aboutMenu.addAction(aboutAct)

        # self.setCentralWidget(self.display)
        centralWidget = QWidget()
        centralWidget.setLayout(grid)
        self.setCentralWidget(centralWidget)
        QTimer.singleShot(0, self.loadFromArgs)

    def updateImage(self):
        img = self.bandManager.getDisplayImage()
        self.display.setImage(img, resetViewport=False)
        self.display.update()

    def setSignal(self, tag, value):
        self.displaySignals[tag] = value
        self.display.update()

    def about(self):
        print("About")

    def clearAnnotations(self):
        answer = QMessageBox.question(self, "Clear Annotations", "Are you sure you want to clear annotations?")
        if answer == QMessageBox.StandardButton.Yes:
            self.display.clearAnnotations()
            self.display.update()

    def loadAnnotation(self):
        if not self.display.loaded:
            QMessageBox.warning(self, "Load Annotation Failed", "No image file is loaded")
            return
        fname, _ = QFileDialog.getOpenFileName(
            self, "Load Annotation File", os.curdir, "Annotation Files (*.pkl)",
            options = QFileDialog.Option.DontUseNativeDialog)
        if not fname:
            return
        err = self.display.loadAnnotations(fname)
        if err:
            QMessageBox.warning(self, "Load Annotation Failed", err)

    def saveAnnotation(self):
        if not self.display.loaded:
            QMessageBox.warning(self, "Save Annotation Failed", "No image file is loaded")
            return
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save Annotation File", os.curdir, "Annotation Files (*.pkl)",
            options = QFileDialog.Option.DontUseNativeDialog)
        if not fname:
            return
        err = self.display.saveAnnotations(fname)
        if err:
            QMessageBox.warning(self, "Save Annotation Failed", err)

    def load(self, unused = None, filename = None):
        if filename is None:
            fname, _ = QFileDialog.getOpenFileName(
                self, "Open HyperSpectral Image", os.curdir, "Image Files (*.jpg *.png *.mat *.pkl)",
                options = QFileDialog.Option.DontUseNativeDialog)
        else:
            fname = filename
        if not fname:
            return
        if fname.endswith('jpg') or fname.endswith('png'):
            try:
                img = numpy.array(Image.open(fname))
            except:
                QMessageBox.warning(self, "Load Failed", "Could not load input image file.")
                return
            self.display.setImage(img)
            self.cube = None
            self.cubeNorm = None
            for c in self.operationControls:
                c.setEnable(False)
            self.setWindowTitle(fname.split('/')[-1])
            return
        isDataLook = False
        if fname.endswith('mat'):
            try:
                print(fname)
                cube = hdf5storage.loadmat(fname)
            except:
                QMessageBox.warning(self, "Load Failed", "Could not load MATLAB file.")
                return
        elif fname.endswith('pkl'):
            # cube = DataLook(fname, verbose = False)
            try:
                with open(fname, 'rb') as fid:
                    cube = pickle.load(fid)
            except:
                QMessageBox.warning(self, "Load Failed", "Could not load pickle file.")
                return
            if isinstance(cube, dict) and 'frame' in cube and 'hypercube' not in cube:
                frame = cube['frame']
                if len(frame.shape) != 2 or frame.shape[0] & 1 or frame.shape[1] & 1:
                    QMessageBox.warning(self, "Load Failed", "Pickle file expected frame with even dimensions.")
                    return
                frame = frame[:, ::2] + frame[:, 1::2].astype('int') * 256
                if frame.shape[1] & 1:
                    QMessageBox.warning(self, "Load Failed", "Pickle file expected frame with even dimensions.")
                    return
                hcube = numpy.zeros((frame.shape[0]//2, frame.shape[1]//2, 4), dtype = frame.dtype)
                hcube[:, :, 0] = frame[::2, ::2]
                hcube[:, :, 1] = frame[1::2, ::2]
                hcube[:, :, 2] = frame[::2, 1::2]
                hcube[:, :, 3] = frame[1::2, 1::2]
                cube['ref'] = hcube
                if 'webcam' in cube:
                    cube['lbl'] = cube['webcam']
            elif 'angle' in cube:
                QMessageBox.warning(self, "Load Failed", "Loading DataLook is not implemented.")
                return
                isDataLook = True
                cube = DataLook(fname)
                if not cube.valid:
                    QMessageBox.warning(self, "Load Failed", "DataLook load failed.")
                    return
        else:
            QMessageBox.warning(self, "Load Failed", "Unexpected file suffix.")
            return
        if isinstance(cube, dict) and not 'hypercube' in cube and not 'ref' in cube:
            QMessageBox.warning(self, "Load Failed", "Expected 'hypercube' or 'ref' field in the input data structure.")
            return
        overviewImage = None
        wavelengths = None
        if isinstance(cube, DataLook):
            _, _, overviewImage = cube[0]
            overviewImage = overviewImage[:16*(cube.shape[0]//16), 16*(cube.shape[1]//16), :]
        elif 'hypercube' in cube:
            if 'wavelengths' in cube:
                wavelengths = cube['wavelengths']
            cube = cube['hypercube']
        else:
            if 'lbl' in cube:
                overviewImage = cube['lbl']
            if 'wavelengths' in cube:
                wavelengths = cube['wavelengths']
            cube = cube['ref']


        if not isinstance(cube, numpy.ndarray) or len(cube.shape) != 3:
            QMessageBox.warning(self, "Load Failed", "Expected 'hypercube' field field to be a 3 dimensional numpy array.")
            return
        if wavelengths is not None and len(wavelengths) != cube.shape[2]:
            QMessageBox.warning(self, "File format problem", "Wavelengths are in file but they have the wrong length.")
            wavelengths = None

        def checkAxis(ax, cube = cube):
            if cube.shape[ax] % 16 != 0:
                print("WARNING: Axis %s must be divisible by 16. padding some edge rows." % ax)
                return 16 - cube.shape[ax] % 16
            else:
                return 0
        if not isDataLook:
            excess0, excess1 = checkAxis(0), checkAxis(1)
            ncube = numpy.zeros((cube.shape[0] + excess0, cube.shape[1] + excess1, cube.shape[2]))
            ncube[:cube.shape[0], :cube.shape[1], :] = cube
            cube = ncube

        overviewControl = None
        for c in self.operationControls:
            c.setEnable(True)
            if c.slider is not None:
                c.slider.setMaximum(cube.shape[2]-1)
                if c.slider.value() > cube.shape[2]-1:
                    c.slider.setValue(0)
                c.slider.setToolTip(str(c.slider.value()))
            else:
                overviewControl = c

        self.setWindowTitle(fname.split('/')[-1])
        self.display.clearAll()
        self.bandManager.setBaseState(noView = True)
        self.controlLayout.reset()
        self.wavelengths = wavelengths
        self.cube = cube
        if overviewImage is not None:
            overviewControl.setOverviewImage(overviewImage, fname, noupdate = True)
        if not isDataLook:
            self.cubeMax = cube.max()
            self.cubeMin = cube.min()
            self.cubeNorm = (cube*cube).sum(axis = 2).flatten()
        self.bandManager.setBaseState(0)
        img = self.bandManager.getDisplayImage()
        self.display.setImage(img)
        for c in self.operationControls:
            c.setEnable(True)
            if c.slider is not None:
                c.setReferenceFromCube()
        self.display.update()

    def extractPointSpectra(self):
        results = []
        for x, y, cls in self.display.annotations['points']:
            x, y = int(x), int(y)
            if x >= 0 and x < self.cube.shape[1] and y >= 0 and y < self.cube.shape[0]:
                results.append(self.cube[y,x,:])
        results = numpy.array(results)
        return results

    def extractRectSpectra(self):
        results = []
        for pt1, pt2, cls in self.display.annotations['rects']:
            clipRow = lambda x, y = self.cube.shape[0]-1: max([0, min([y, x])])
            clipCol = lambda x, y = self.cube.shape[1]-1: max([0, min([y, x])])
            xmin, xmax = clipCol(min([pt1[0], pt2[0]])), clipCol(max([pt1[0], pt2[0]]))
            ymin, ymax = clipRow(min([pt1[1], pt2[1]])), clipRow(max([pt1[1], pt2[1]]))
            rows = numpy.arange(self.cube.shape[0])
            cols = numpy.arange(self.cube.shape[1])
            rows = (rows >= ymin) & (rows <= ymax)
            cols = (cols >= xmin) & (cols <= xmax)
            results.append(self.cube[rows, :, :][:,cols,:].reshape((-1, self.cube.shape[2])))
        return results

    def _plotDict(self, results, errMsg):
        plots = []
        titles = []
        for ky in results:
            if len(results[ky]) > 0:
                plots.append(numpy.array(results[ky]).T)
                titles.append(ImageWindow.ClassNames[ky])
        if len(plots) > 0:
            plot(*plots, title = titles, x = self.wavelengths)
        else:
            QMessageBox.warning(self, 'Empty Plot', errMsg)

    def plotPointSpectra(self):
        spectra = self.extractPointSpectra()
        results = collections.defaultdict(list)
        for sp, (_, _, cls) in zip(spectra, self.display.annotations['points']):
            results[cls].append(sp)
        self._plotDict(results, "No points in annotations")

    def plotRectSpectra(self):
        spectra = self.extractRectSpectra()
        results = collections.defaultdict(list)
        for sp, (_, _, cls) in zip(spectra, self.display.annotations['rects']):
            results[cls].append(sp.mean(axis = 0))
        self._plotDict(results, "No rects in annotations")

    def saveAnnotationSpectra(self):
        if self.cube is None:
            QMessageBox.warning(self, "Save Annotation Spectra", "No hyperspectral cube is loaded")

        results = {'point_spectra': self.extractPointSpectra(), 'rect_spectra': self.extractRectSpectra()}
        results.update(self.display.annotations)
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save Annotation Spectrum File", os.curdir, "Annotation Files (*.pkl)",
            options = QFileDialog.Option.DontUseNativeDialog)
        if not fname:
            return
        try:
            with open(fname, 'wb') as fid:
                pickle.dump(results, fid)
        except:
            QMessageBox.warning(self, "Save Annotation Spectra", "Failed to save output spectra")

if __name__ == '__main__':

    sys.path.append('')
    parser = argparse.ArgumentParser()

    parser.add_argument('file', help = 'Name of file to open with', nargs = '?', default = None)

    args = parser.parse_args()

    app = QApplication(sys.argv)

    win = MainWindow(args)
    availableGeometry = win.screen().availableGeometry()
    win.resize(availableGeometry.width() // 3, availableGeometry.height() // 2)
    win.show()

    sys.exit(app.exec())
