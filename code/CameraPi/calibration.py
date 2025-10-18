import numpy
import cv2
from PIL import Image
import scipy.interpolate
import scipy.ndimage
from databaseUtilities import convertToUint8
import pickle

polynomial32 = [32, 22, 2, 1, 0]

def makeFill(polynomial):
    return numpy.ones(polynomial.max(), dtype='int')

def checkerboard(rows, cols, chip):
    assert (rows & 1 == 0 and cols & 1 == 0)
    x, y = numpy.meshgrid(numpy.arange(cols), numpy.arange(rows))
    return numpy.kron(numpy.mod(x + y, 2) * 255, numpy.ones((chip, chip))).astype('uint8')


def lfsr(N, initial=None, polynomial=polynomial32):
    polynomial = numpy.array(polynomial)
    assert (polynomial.min() == 0 and len(polynomial.shape) == 1)
    assert (numpy.unique(polynomial).shape[0] == polynomial.shape[0])
    if initial is None:
        initial = makeFill(polynomial)
    initial = numpy.array(initial).astype('int')
    startFill = initial.copy()
    degree = polynomial.max()
    assert (len(initial.shape) == 1 and degree == initial.shape[0])
    assert (set(numpy.unique(initial)).issubset({0, 1}))
    taps = numpy.array([x for x in polynomial if x != degree])
    mask = numpy.zeros(degree, dtype='int')
    mask[taps] = 1
    result = numpy.zeros(N, dtype='int')
    for i in range(N):
        result[i] = initial[0]
        nval = (initial * mask).sum() & 1
        initial = numpy.roll(initial, -1)
        initial[-1] = nval
        # print(initial)
        # if numpy.all(initial == startFill):
        #    print(result)
        #    raise Exception("Cycle found: %d" % (i+1))

    return result, startFill, polynomial


def buildImages(rows, cols, nImages, offset):
    imgSize = rows * cols
    N = imgSize * nImages + offset
    seq, ini, poly = lfsr(N)
    result = []
    for i in range(nImages):
        result.append(seq[offset:(offset + imgSize)].reshape((rows, cols)))
        offset += imgSize
    return numpy.array(result), ini, poly, offset


def saveImages(imgs, outFmt, chipSize):
    chip = numpy.ones((chipSize, chipSize), dtype='int')
    for i in range(imgs.shape[0]):
        im = Image.fromarray(numpy.kron(imgs[i, :, :], chip).astype('uint8') * 255)
        im.save(outFmt % i)


def extractBigConnectedComponent(mask, threshold, minimumPixelFraction, ignoreFraction):
    mx, mn = numpy.percentile(mask, [100 - ignoreFraction, ignoreFraction])
    mask = numpy.clip((mask - mn) / (mx - mn), 0, 1.0)
    mask = (mask > threshold).astype('uint8')
    N, res = cv2.connectedComponents(mask)
    y, x = numpy.histogram(res.flatten(), numpy.linspace(-0.5, N + 1, N))
    nx = numpy.round(x[:-1] + 0.5).astype('int')
    npix = mask.shape[0] * mask.shape[1] * minimumPixelFraction
    pos = y > npix
    ny = y[pos]
    nx = nx[pos]
    blueClass = []
    for c in nx:
        if mask[(res == c)].mean() > threshold:
            blueClass.append(c)
    if len(blueClass) != 1:
        print(blueClass)
        raise Exception('Could not find blue field.')
    blueClass = blueClass[0]
    blueMask = numpy.zeros(mask.shape, dtype='uint8')
    blueMask[res == blueClass] = 255
    # blueMask = cv2.dilate(blueMask, numpy.ones((3,3)))
    blueMask = cv2.erode(blueMask, numpy.ones((15, 15)))
    blueMask = cv2.dilate(blueMask, numpy.ones((15, 15)))
    return blueMask


def extractRawBlueField(frame, threshold=0.5, minimumPixelFraction=0.05, ignoreFraction=0.25):
    res = []
    for i in range(4):
        res.append(cv2.GaussianBlur(frame[i, :, :].astype('double'), (7, 7), 0))
    mask = res[3] / (res[0] + (res[1] + res[2]) / 2 + 1)
    return extractBigConnectedComponent(mask, threshold, minimumPixelFraction, ignoreFraction)


def extractBlueField(img, minimumPixelFraction=0.05, threshold=0.5, ignoreFraction=0.25):
    mask = img[:, :, 2] / (img[:, :, 0].astype('float') + img[:, :, 1].astype('float') + 1)
    return extractBigConnectedComponent(mask, threshold, minimumPixelFraction, ignoreFraction)

def findPatternParameters(frame, mask, rows, cols, pattern, patternSize, guardBand = 10):
    y, x = rows.mean(), cols.mean()
    M = numpy.vstack((cols - x, rows - y)).T
    u, d, v = numpy.linalg.svd(M)
    for i in range(2):
        if v[i,i] < 0: v[i,:] = -v[i,:]
    center = numpy.array([x,y])
    pts = v.dot(M.T)
    mn = pts.min(axis = 1)
    mx = pts.max(axis = 1)
    dims = mx - mn
    pixelsPerInch = dims/patternSize
    pixelsPerChip = dims/numpy.array(pattern.shape[::-1])
    meanPPI = pixelsPerInch.mean()
    meanPPC = pixelsPerChip.mean()
    top, bottom = rows.min() - guardBand, rows.max() + guardBand
    left, right = cols.min() - guardBand, cols.max() + guardBand
    subMask = mask[top:bottom+1, left:right+1]
    subFrame = frame[top:bottom+1, left:right+1]
    mid = int((subFrame.max()-subFrame.min())/2)
    subFrame[subMask!=0] = mid
    subFrame[:, :guardBand] = mid
    subFrame[:, -guardBand:] = mid
    subFrame[:guardBand, :] = mid
    subFrame[-guardBand:, :] = mid
    angle = numpy.arctan2(v[0,1], v[0,0])*180/numpy.pi
    subFrame = scipy.ndimage.rotate(subFrame, angle)
    return meanPPI, meanPPC, pixelsPerInch, pixelsPerChip, center, v, subFrame, angle

def buildPattern(input, pixelsPerChip, method = 'linear'):
    input = 2*((input.astype('double') - input.min())/(input.max()-input.min()))-1
    newRows = numpy.linspace(0, input.shape[0]-1, int(numpy.round(input.shape[0]*pixelsPerChip)))
    newCols = numpy.linspace(0, input.shape[1]-1, int(numpy.round(input.shape[1]*pixelsPerChip)))
    interp = scipy.interpolate.RegularGridInterpolator((numpy.arange(input.shape[0]), numpy.arange(input.shape[1])), input, method = method)
    x, y = numpy.meshgrid(newCols, newRows)
    return interp((y, x))

def cropFrame(frame, outSize, row, col):
    return numpy.roll(numpy.roll(frame, -row, axis = 0), -col, axis = 1)[:outSize[0],:outSize[1]]

def matchPatternToFrame(frame, pattern, maxShift):
    assert(frame.shape[0] > pattern.shape[0] and frame.shape[1] > pattern.shape[1])
    center = numpy.array(frame.shape)/2 + 0.5
    upperLeft = numpy.round(center - numpy.array([pattern.shape[0]/2, pattern.shape[1]/2])).astype('int')
    p = pattern.flatten()
    result = numpy.zeros((2*maxShift + 1)**2)
    pos = 0
    for yShift in range(-maxShift, maxShift + 1):
        for xShift in range(-maxShift, maxShift + 1):
            result[pos] = p.dot(cropFrame(frame, pattern.shape, upperLeft[0] + yShift, upperLeft[1] + xShift).flatten())
            pos += 1
    return result.reshape((2*maxShift + 1, 2*maxShift + 1))/(2*maxShift+1)**2

def findChessCorners(gray, dims, cornerSize = 5, threshold = 95, doNorm = False, partial = False, verbose = False):
    def neighbors(idx, d, cents):
        p = numpy.argsort(d)
        d = d[p]
        center = cents[idx,:]
        assert(d[0] < 1e-4)
        assert(p[0] == idx)
        sumDist = 0
        nDist = 0
        avgDist = 0
        left, right, up, down = None, None, None, None
        for i, (j, dst) in enumerate(zip(p[1:5], d[1:5])):
            if i < 2 or dst < avgDist * 1.3:
                delta = cents[j,:]-center
                if abs(delta[1]) > abs(delta[0]):
                    if delta[1] < 0:
                        assert(left is None)
                        left = j
                    else:
                        assert(right is None)
                        right = j
                else:
                    if delta[0] < 0:
                        assert(up is None)
                        up = j
                    else:
                        assert(down is None)
                        down = j
                nDist += 1
                sumDist += dst
                avgDist = sumDist/nDist
        return left, right, up, down

    corner = numpy.kron(numpy.array([[1, -1], [-1, 1]]), numpy.ones((cornerSize, cornerSize)))
    if doNorm:
        nrm = cv2.filter2D(gray, cv2.CV_64F, numpy.ones((4*cornerSize, 4*cornerSize)))
    else:
        nrm = numpy.ones(gray.shape)
    f = cv2.filter2D(gray/nrm, cv2.CV_64F, corner)**2
    thresh = numpy.percentile(numpy.abs(f), threshold)
    peaks = numpy.where(f>thresh, f, 0)
    if partial:
        return peaks
    N, cmps = cv2.connectedComponents((peaks>0).astype('uint8'))
    if N != (1 + dims[0]*dims[1]):
        print(dims, N)
        raise Exception("Number of connected components does not match requested size. Try adjusting parameters.")

    centroids = []
    for c in range(N):
        current = cmps == c
        cnt = current.sum()
        if cnt > numpy.prod(cmps.shape)/2:
            continue
        rows, cols = numpy.nonzero(current)
        centroid = numpy.array([0,0],dtype = 'float64')
        sm = 0
        for r, c in zip(rows, cols):
            centroid += numpy.array([r, c])*peaks[r,c]
            sm += peaks[r,c]
        centroids.append(centroid/sm)
    assert(len(centroids) == N-1)
    centroids = numpy.array(centroids)
    d2 = (centroids**2).sum(axis = 1)
    distances = numpy.sqrt(numpy.abs(-2*centroids.dot(centroids.T) + d2.reshape((1,-1)) + d2.reshape((-1,1))))
    lrud = []
    for c in range(centroids.shape[0]):
        lrud.append(neighbors(c, distances[c], centroids))

    bottomLeft = None
    for idx, (l, r, u, d) in enumerate(lrud):
        if l is None and d is None:
            assert(bottomLeft is None)
            bottomLeft = idx
    currentBottom = bottomLeft
    current = bottomLeft
    centroidsInOrder = []
    for i in range(dims[1]):
        for j in range(dims[0]):
            if verbose: print(j, dims[0], current)
            centroidsInOrder.append(current)
            current = lrud[current][2]
            if verbose: print(" ^", current)
        assert(current is None)
        current = lrud[currentBottom][1]
        if verbose: print(">",  current)
        currentBottom = current
    assert(current is None)
    centroids = centroids[numpy.array(centroidsInOrder), :]
    return peaks, centroids

def drawCentroids(gray, centroids, radius = 2, number = False, scale = 1.0, skip = 5):
    tmp = convertToUint8(gray)
    detected = numpy.zeros(tmp.shape + (3,), dtype = gray.dtype) + numpy.transpose(numpy.array((tmp, tmp, tmp)), (1,2,0))
    for idx, c in enumerate(centroids):
        detected  = cv2.circle(detected, c[::-1].astype('int'), radius, (255, 0, 0))
        if number and idx % skip == 0:
            detected = cv2.putText(detected, str(idx), c[::-1].astype('int'),cv2.FONT_HERSHEY_SIMPLEX,
                                   scale, (0,0,255), 2, cv2.LINE_AA)
    for i in range(1, centroids.shape[0]):
        detected = cv2.line(detected, centroids[i-1,::-1].astype('int'), centroids[i, ::-1].astype('int'), (0, 255, 0))
    return detected


class DistortionCorrection(object):
    def __init__(self):
        self.sourceDimensions = None
        self.degree = 1
        self.coeffs = numpy.array([[0, 1, 0], [0, 0, 1]])

    def _buildDesignMatrix(self, coords):
        x = coords[:, 1]
        y = coords[:, 0]
        columns = []
        for d in range(self.degree + 1):
            for px in range(d + 1):
                if px != 0:
                    cx = x ** px
                else:
                    cx = numpy.ones(x.shape[0])
                if d != px:
                    cy = y ** (d - px)
                else:
                    cy = numpy.ones(y.shape[0])
                columns.append(cx * cy)
        return numpy.vstack(columns).T

    def regressFromData(self, sourceCoords, destinationCoords, degree, sourceDimensions = None):
        """Compute a map from square coordinates to distorted coordinates at a given degree.
        coordinates are row/col
        """
        assert (degree >= 1)
        assert (sourceCoords.shape == destinationCoords.shape)
        assert (sourceCoords.shape[1] == 2)
        if sourceDimensions is not None:
            self.sourceDimensions = numpy.array(sourceDimensions)
            assert(self.sourceDimensions.shape == (2,))
        else:
            self.sourceDimensions = None
        self.degree = degree
        # Get the design matrix for the given degree
        A = self._buildDesignMatrix(sourceCoords)
        assert (A.shape[1] <= sourceCoords.shape[0])
        # Normalize the columns. We solve the linear system inv(D)(A'*A)inv(D) x = inv(D) A'*y
        # This means x = D(inv(A'A)*A'*y) so the desired coefficients are inv(D)*x
        scale = A.std(axis=0)
        scale = numpy.where(scale == 0, 1, scale)
        # We improve the condition of A by setting a scale
        iD = numpy.diag(1 / scale)
        As = A.dot(iD)
        preCoeffs, _, rank, _ = numpy.linalg.lstsq(As, destinationCoords, rcond = None)
        if rank < As.shape[1]:
            print("WARNING: Rank of input is %d." % rank)
        self.coeffs = iD.dot(preCoeffs)

    def __call__(self, coords):
        return self._buildDesignMatrix(coords).dot(self.coeffs)

    def save(self, fname, additional_data = {}):
        with open(fname, 'wb') as fid:
            pickle.dump({'calibration': self, 'additional': additional_data}, fid)

    @classmethod
    def open(cls, fname):
        with open(fname, 'rb') as fid:
            data = pickle.load(fid)
        assert (isinstance(data, dict) and 'calibration' in data and isinstance(data['calibration'], cls))
        return data['calibration'], data['additional'], data['hsiInfo'] if 'hsiInfo' in data else None

    @classmethod
    def updateAdditionalData(cls, fname, tag, new_data):
        calibration, data = cls.open(fname)
        data[tag] = new_data
        calibration.save(fname, data)