import sqlite3
import os
import subprocess
import argparse
import tempfile
import time
import shutil
import pickle
import datetime
from PIL import Image
import numpy
import tempfile
import webbrowser
import scipy.signal
import scipy.ndimage

OUTPUT_DIRECTORY_TAG = 'CAMERA_OUTPUT_DIRECTORY'
CAMERA_IMAGES_DIR = '/home/X/Documents/Hyperspectral/Images'
COMPUTER_IMAGES_DIR = '/Volumes/HyperspectralData/Images'
CAMERA_HOSTS = { 1: 'camerapi.local',
                 2: 'camerapi2.local'}
COMPUTER_HOST = 'mac-mini.local'
DATABASE_NAME = 'image_database.db'
SERVER_PORT = 8080


class DataLook(object):
    def __init__(self, fname, camera_id = 2, hsiInfoName=None, infoTag='', verbose = True):
        self.valid = False
        if isinstance(fname, int):
            files = getFilesById(fname, computer_dir=COMPUTER_IMAGES_DIR + str(camera_id))
            if len(files) != 1:
                if verbose: print("File id, %s, and camera id, %s, combination is not valid" % (fname, camera_id))
                return
            fname = files[0]

        if not os.path.exists(fname):
            if verbose: print("Path %s not found." % fname)
            return
        self.fid = open(fname, 'rb')
        pos = [0]
        try:
            self.base = pickle.load(self.fid)
        except:
            if verbose: print("Failed to load %s as a pickle file." % fname)
            return
        if not isinstance(self.base, dict) or 'angle' not in self.base:
            if verbose: print("Input file %s is not a data collect." % fname)
            return
        count = 0
        while True:
            pos.append(self.fid.tell())
            d = pickle.load(self.fid)
            if count == 0:
                if 'capCam' in d:
                    self.version = 0
                elif 'raw' in d:
                    self.version = 1
                elif 'picam' in d:
                    self.version = 2
            if 'success' in d:
                break
            else:
                count += 1

        self.nframes = count
        self.positions = pos
        self.loadHSIInfo(hsiInfoName, infoTag)
        self.valid = True
        _, _, img = self[0]
        self.shape = img.shape
    def __len__(self):
        return self.nframes

    def __del__(self):
        if self.valid:
            self.fid.close()

    def _getStructInfo(self, d):
        if 'webcam' in d:
            imgWeb = Image.fromarray(d['webcam'][:, :, ::-1])
        else:
            imgWeb = numpy.squeeze(d['frameLine'])
        if self.version == 0:
            imgSpect = d['capCam']
            return d, imgWeb, imgSpect
        elif self.version == 1:
            import rawproc
            buffer = d['raw']['raw']
            recon = rawproc.procdng(buffer, len(buffer))
            imgSpect = numpy.array(recon.image).reshape(recon.height, recon.width, recon.bands)
            d['capCam'] = numpy.round(255*imgSpect.astype('float')/imgSpect.max()).astype('uint8')
            return d, imgWeb, imgSpect
        elif self.version == 2:
            imgSpect = deBayerRaw(d['picam'])
            if d['metadata']['windowed']:
                imgSpect = imgSpect[2:-2,2:-2,:]
            d['capCam'] = numpy.round(255*imgSpect.astype('float')/imgSpect.max()).astype('uint8')
            return d, imgWeb, imgSpect

    def __getitem__(self, idx):
        if idx >= self.nframes:
            raise Exception("Index %d is past the end of array." % idx)
        self.fid.seek(self.positions[idx + 1])
        d = pickle.load(self.fid)
        d, imgWeb, imgSpect = self._getStructInfo(d)
        return d, imgWeb, imgSpect

    def getRawData(self, idx):
        if idx >= self.nframes:
            raise Exception("Index %d is past the end of array." % idx)
        self.fid.seek(self.positions[idx + 1])
        d = pickle.load(self.fid)
        return d

    def __iter__(self):
        self.fid.seek(self.positions[1])
        for i in range(self.nframes):
            d = pickle.load(self.fid)
            d, imgWeb, imgSpect = self._getStructInfo(d)
            yield d, imgWeb, imgSpect

    def asImage(self, idx):
        if idx < 0 or idx >= self.nframes:
            print("Index out of range.")
            return
        _, _, img = self[idx]
        if isinstance(img, numpy.ndarray):
            img = img.astype('float')
            return Image.fromarray(numpy.round(255*img/img.max()).astype('uint8'))
        else:
            return img

    @staticmethod
    def createHSIInfoFromAnnotations(annotationFile=None, classId=1):
        if annotationFile is None or classId is None:
            raise Exception("When creating an HSI data structure you must specify a annotation file and a class.")
        with open(annotationFile, 'rb') as fid:
            annots = pickle.load(fid)
        spect1 = None
        for p1, p2, cl in annots['rects']:
            if cl == classId:
                spect1 = p1
                spect2 = p2
                break
        if spect1 is None:
            raise Exception("Could not find rectangle with class %d in %s." % (classId, annotationFile))
        ul = numpy.round(spect1).astype('int')
        lr = numpy.round(spect2).astype('int')
        centerColumn = 320
        fov25 = [103, 318]
        focalLength = 25
        centerFov = 320
        fov = [int(fov25[0] * 25 / focalLength + 0.5), int(fov25[1] * 25 / focalLength + 0.5)]
        result = {}
        result['fov'] = fov
        result['centerFov'] = centerColumn
        result['ul'] = ul
        result['lr'] = lr
        result['wavelengths'] = None
        with open('../Docs/IMX477-Responsivity-Calib.pkl', 'rb') as fid:
            result['responsivity'] = pickle.load(fid)

        return result

    def loadHSIInfo(self, data, tag=''):
        if data is None:
            self.fov, self.centerFov, self.ul, self.lr, self.wavelengths, self.responsivity = None, None, None, None, None, {}
            return

        if isinstance(data, str):
            with open(data, 'rb') as fid:
                data = pickle.load(fid)
        if not isinstance(data, dict):
            raise Exception("Data must be a dictionary or a pickle file from which a dictionary is loaded.")
        if len(tag) > 0:
            for t in tag.split(':'):
                if not t in data:
                    raise Exception("Tag %s in path %s is not in data directionary" % (t, tag))
                data = data[t]
        requiredTags = {'fov', 'centerFov', 'ul', 'lr'}
        if not requiredTags.issubset(data.keys()):
            raise Exception("HSI info data requires tags: %s" % requiredTags)
        self.fov, self.centerFov, self.ul, self.lr = data['fov'], data['centerFov'], data['ul'], data['lr']
        self.wavelengths, self.responsivity = data['wavelengths'], data['responsivity']
        if len(self.wavelengths) != self.lr[0]-self.ul[0]:
            print("Wavelength array does not match the spectrum bounding box. Ignoring wavelengths.")
            self.wavelengths, self.responsivity = None, None

    def asHSI(self, fname, forceSimpleGray = False):
        hsi = numpy.zeros((self.lr[1] - self.ul[1], len(self), self.lr[0] - self.ul[0]))
        refn = None

        if self.responsivity is None or forceSimpleGray:
            def convertToGray(im):
                if isinstance(im, Image.Image):
                    im = numpy.array(im)
                assert (len(im.shape) == 3 and im.shape[2] == 3)
                return numpy.einsum('ijk,k->ij', im, numpy.array([0.2989, 0.5870, 0.1140]))
        else:
            sqr = (self.responsivity*self.responsivity).sum(axis = 0).reshape((1, -1))
            def convertToGray(im, resp = self.responsivity, sqr = sqr):
                assert (len(im.shape) == 3 and im.shape[2] == 3 and im.shape[1] == resp.shape[1] and resp.shape[0] ==3)
                return numpy.einsum('ijk,kj->ij', im, resp)/sqr

        for idx, (d, imgWeb, imgCap) in enumerate(self):
            if self.version < 2 or not d['metadata']['windowed']:
                imgCap = imgCap[self.ul[1]:self.lr[1], self.ul[0]:self.lr[0], :]
            img = convertToGray(imgCap)

            if isinstance(imgWeb, Image.Image):
                imgWC = numpy.array(imgWeb)[self.fov[0]:self.fov[1], self.centerFov, ::-1]
            else:
                imgWC = d['frameLine'][:, ::-1].astype('uint8')

            if refn is None:
                refn = numpy.zeros((imgWC.shape[0], len(self), 3), dtype = 'uint8')
            # Reverse row because Image is upside down due to telescope
            # Reverse column because spectrum is to the left of center (red is leftmost)
            hsi[:, hsi.shape[1] - 1 - idx, :] = img[::-1, ::-1]
            # The image from the collect is BGR
            refn[:, refn.shape[1] - 1 - idx, :] = imgWC

        def checkAxis(ax, cube=hsi):
            if cube.shape[ax] % 16 != 0:
                print("WARNING: Axis %s must be divisible by 16. padding some edge rows." % ax)
                return 16 - cube.shape[ax] % 16
            else:
                return 0

        excess0, excess1 = checkAxis(0), checkAxis(1)
        hsi2 = numpy.zeros((hsi.shape[0] + excess0, hsi.shape[1] + excess1, hsi.shape[2]))

        hsi2[:hsi.shape[0], :hsi.shape[1], :] = hsi

        refn = numpy.array(Image.fromarray(refn).resize((hsi2.shape[1], hsi2.shape[0])))

        with open(fname, 'wb') as fid:
            pickle.dump({'ref': hsi2, 'lbl': refn, 'wavelengths': self.wavelengths}, fid)

    def ascube(self, typ='capCam'):
        cube = None
        for idx, (d, i1, i2) in enumerate(self):
            if typ.lower() == 'capcam':
                img = numpy.array(Image.fromarray(d['capCam']).convert('L'))
            elif isinstance(i1, Image.Image):
                img = numpy.array(i1.convert('L').resize((320, 240)))
            else:
                raise Exception("Do not know how to do it with this Datalook")
            if cube is None:
                cube = numpy.zeros((img.shape[0], img.shape[1], self.nframes), dtype='uint8')
            cube[:, :, idx] = img
        return cube

    def asMovie(self, fname, typ='capCam', brightness=1, contrast=1, startFrame = 0, endFrame = 1e300, individualContrast = False):
        import imageio
        import cv2
        from PIL import ImageEnhance
        wrt = imageio.get_writer(fname)

        def cvt(x):
            if not individualContrast:
                enh = ImageEnhance.Contrast(x.resize((320, 240)))
                enh = ImageEnhance.Brightness(enh.enhance(contrast))
                return numpy.array(enh.enhance(brightness))
            else:
                x = numpy.array(x.resize((320, 240))).astype('float')
                return numpy.round(255*(x-x.min())/max([x.max() - x.min(), 1])).astype('uint8')

        for idx, (d, i1, i2) in enumerate(self):
            if idx < startFrame or idx > endFrame:
                continue
            if typ.lower() == 'capcam':
                img = cvt(Image.fromarray(d['capCam']))
            elif isinstance(i1, Image.Image):
                img = cvt(i1)
            else:
                raise Exception("Don't know what to do with this DataLook")

            cv2.putText(img, str(idx), (10, 230), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)
            wrt.append_data(img)
        wrt.close()

    def saveAsHyperCube(self, fname, typ='capCam'):
        with open(fname, 'wb') as fid:
            pickle.dump({'hypercube': self.ascube(typ)}, fid, pickle.HIGHEST_PROTOCOL)

def convertToUint8(img):
    if not isinstance(img, numpy.ndarray):
        raise Exception("Input to convertToUint8 must be a numpy array")
    if img.dtype == 'uint8':
        return img
    else:
        return (255*(img-img.min())/(img.max()-img.min())).astype('uint8')

def convertRawTo12Bits(frame):
    return frame.astype('uint8').view(numpy.uint16).astype('int')

def deBayerRaw(frame):
    frame = frame.astype('float') - 256
    horiz = numpy.clip(scipy.signal.convolve2d(frame, numpy.array([[0, 0, 0],[1, 0, 1], [0, 0, 0]])/2, mode = 'same'), 0, 65536)
    vert = numpy.clip(scipy.signal.convolve2d(frame, numpy.array([[0, 1, 0],[0, 0, 0],[0, 1, 0]])/2, mode = 'same'), 0, 65536)
    diag = numpy.clip(scipy.signal.convolve2d(frame, numpy.array([[1, 0, 1],[0, 0, 0],[1, 0, 1]])/4, mode = 'same'), 0, 65536)
    cross = numpy.clip(scipy.signal.convolve2d(frame, numpy.array([[0, 1, 0],[1, 0, 1],[0, 1, 0]])/4, mode = 'same'), 0, 65536)
    result = numpy.zeros((frame.shape + (3, )), dtype = 'float')
    # Red upper left
    result[::2, ::2, 0] = frame[::2, ::2]
    result[::2, ::2, 1] = cross[::2, ::2]
    result[::2, ::2, 2] = diag [::2, ::2]

    # Green upper right
    result[::2, 1::2, 0] = horiz[::2, 1::2]
    result[::2, 1::2, 1] = frame[::2, 1::2]
    result[::2, 1::2, 2] = vert [::2, 1::2]

    # Green lower left
    result[1::2, ::2, 0] = vert [1::2, ::2]
    result[1::2, ::2, 1] = frame[1::2, ::2]
    result[1::2, ::2, 2] = horiz[1::2, ::2]

    # Blue lower right
    result[1::2, 1::2, 0] = diag [1::2, 1::2]
    result[1::2, 1::2, 1] = cross[1::2, 1::2]
    result[1::2, 1::2, 2] = frame[1::2, 1::2]
    return result


def convertRawToArray(frame):
    if isinstance(frame, dict):
        if frame['frame'] is None:
            return frame['frameRGB']
        else:
            frame = frame['frame']

    nframe = convertRawTo12Bits(frame)
    red = nframe[::2,::2]
    green1 = nframe[1::2,::2]
    green2 = nframe[::2,1::2]
    blue = nframe[1::2,1::2]
    return numpy.array([red, green1, green2, blue])

def frameBands():
    return ['red', 'green', 'green', 'blue']

def loadCameraRaw(fname):
    with open(fname, 'rb') as fid:
        frame = pickle.load(fid)
    return convertRawToArray(frame), frame

def loadRawById(iden, camera_id = 1):
    files = getFilesById(iden, computer_dir = COMPUTER_IMAGES_DIR + str(camera_id))
    if len(files) != 1:
        raise Exception("Cannot find ID %d" % iden)
    return loadCameraRaw(files[0])

def loadCameraFrame(f):
    if f.endswith('png'):
        return numpy.array(Image.open(f))
    elif f.endswith('pkl'):
        data, _ = loadCameraRaw(f)
        return data

def loadCameraImage(f, band = 0):
    img = loadCameraFrame(f)
    if img.shape[0] == 4:
        return Image.fromarray(convertToUint8(img)[band,:,:])
    else:
        return Image.fromarray(img)

def displayFrame(frame):
    fname = tempfile.gettempdir() +'/f%s_%s.jpg' % (time.time(), os.getpid())
    Image.fromarray(convertToUint8(frame)).save(fname)
    webbrowser.open('file://%s' % fname)

def remoteDisplayFrame(frame):
    fname = tempfile.gettempdir() +'/f%s_%s.jpg' % (time.time(), os.getpid())
    Image.fromarray(convertToUint8(frame)).save(fname)
    print(fname)
    os.system("curl -F 'image=@%s'  http://Mac-mini.local:8181/display_image"%fname)
    os.remove(fname)


def checkOutputDirectory(OutputDirectoryTag = OUTPUT_DIRECTORY_TAG, MacDefault = COMPUTER_IMAGES_DIR , PiDefault = CAMERA_IMAGES_DIR, camera_id = None):
    odir = None
    if OutputDirectoryTag in os.environ:
        odir = os.environ[OutputDirectoryTag]
    elif os.path.isdir(PiDefault):
        odir = PiDefault
    else:
        if camera_id is None:
            for k in CAMERA_HOSTS:
                if os.path.isdir(MacDefault + str(k)):
                    odir = MacDefault + str(k)
                    break
        else:
            odir = MacDefault + str(camera_id)
            if not os.path.isdir(odir):
                raise Exception("Directory %s not found." % odir)
    if odir is None:
        raise Exception("Could not find output directory.")
    if not os.path.exists(odir):
        os.mkdir(odir)
    if not os.path.isdir(odir):
        raise Exception("Could not create an output directory: %s" % odir)
    imageDB = odir + '/image_database.db'
    conn = sqlite3.connect(imageDB)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    if not {'info', 'images'}.issubset(set([x[0] for x in tables])):
        cursor.execute('create table info (key text, value text)')
        cursor.execute('create table images (id int, name text, timestamp text, comment text)')
        cursor.execute("insert into info values ('last_image', '0')")
        conn.commit()
    conn.close()
    return odir, imageDB

def saveOutput(frame, comment = '', camera_id = None):
    odir, imageDB = checkOutputDirectory(camera_id = camera_id)
    suffix = 'png'
    if isinstance(frame, (dict, str)):
        suffix = 'pkl'
    conn = sqlite3.connect(imageDB)
    cursor = conn.cursor()
    cursor.execute("select value from info where key = 'last_image'")
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise Exception("Database corrupted. Cannot find last image recorded.")
    currentImg = int(rows[0][0]) + 1
    cursor.execute("update info set value = '%s' where key = 'last_image'" % currentImg)
    basename = 'IMG_%05d.%s' % (currentImg, suffix)
    fname = odir + '/' + basename
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')
    cursor.execute("insert into images values (%d, '%s', '%s', '%s')" % (currentImg, basename, timestamp, comment))
    conn.commit()
    conn.close()
    if isinstance(frame, str):
        if os.path.exists(frame):
            shutil.move(frame, fname)
        else:
            print("File creation failed.")
            with open(fname, 'wb') as fid:
                pickle.dump({'failed_collect': True}, fid, protocol=pickle.HIGHEST_PROTOCOL)
    elif isinstance(frame, dict):
        with open(fname, 'wb') as fid:
            pickle.dump(frame, fid, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        img = Image.fromarray(frame)
        img.save(fname)
    return fname

def getDataFromDB(computer_dir = COMPUTER_IMAGES_DIR, dbname = DATABASE_NAME, camera_id = None):
    if camera_id is not None:
        dbname = computer_dir + str(camera_id) + '/' + dbname
    else:
        dbname = computer_dir + '/' + dbname
    if not os.path.exists(dbname):
        existing_files = []
    else:
        conn = sqlite3.connect(dbname)
        cursor = conn.cursor()
        cursor.execute('select id, name, timestamp, comment from images order by timestamp')
        existing_files = cursor.fetchall()
        conn.close()
    return existing_files, dbname

def getFilesById(iden = None, fname = None, computer_dir = COMPUTER_IMAGES_DIR, dbname = DATABASE_NAME):
    files, _ = getDataFromDB(computer_dir, dbname)
    ifname = []
    if iden is not None:
        for i, f, _, _ in files:
            if i == iden:
                ifname.append(f)
    elif fname is not None:
        for i, f, _, _ in files:
            if f.find(fname) >= 0:
                ifname.append(f)
    return [computer_dir + '/' + f for f in ifname]

def synchronizeFromComputer(source_dir = CAMERA_IMAGES_DIR, dest_dir = None,
                            dbname = DATABASE_NAME, dryrun = False, source_host = CAMERA_HOSTS[1]):
    output_dir_name = dest_dir
    if not os.path.exists(output_dir_name):
        os.mkdir(output_dir_name)
    if not os.path.isdir(output_dir_name):
        raise Exception("Output file %s is not a directory.")
    remote_db = source_host + ':' + source_dir +'/' + dbname
    existing_files, dbname = getDataFromDB(output_dir_name, dbname)
    tmp_db_name = tempfile.gettempdir() + '/tmpdb-%s-%s.db' % (time.time(), os.getpid())
    proc = subprocess.Popen(['scp', remote_db, tmp_db_name], stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
    proc.communicate()
    if proc.returncode != 0:
        raise Exception("Failed to get remote DB: %s" % remote_db)
    conn = sqlite3.connect(tmp_db_name)
    cursor = conn.cursor()
    cursor.execute('select id, name, timestamp, comment from images order by timestamp')
    remote_files = cursor.fetchall()
    conn.close()
    local_files = dict([(f, d) for _, f, d, _ in existing_files])
    dbupdate = False
    for _, f, d, _ in remote_files:
        if not f in local_files or local_files[f] != d:
            remote_img = source_host + ':' + source_dir + '/' + f
            local_img = output_dir_name + '/' + f
            dbupdate = True
            print("Retrieving %s to %s..." % (remote_img, local_img))
            if not dryrun:
                proc = subprocess.Popen(['scp', remote_img, local_img], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                proc.communicate()
                if proc.returncode != 0:
                    print("  Transfer failed.")
            else:
                print("  Dry run. No copy done.")

    if dbupdate:
        if not dryrun:
            old_dbs_dir = output_dir_name + '/' + 'OldDBs'
            if not os.path.isdir(old_dbs_dir):
                os.mkdir(old_dbs_dir)
            backup = old_dbs_dir + '/old-db-' + datetime.datetime.now().strftime('%Y-%m-d-%H-%M-%S') + '.db'
            old_files = sorted([f for f in os.listdir(old_dbs_dir) if f.startswith('old-db-')])
            if len(old_files) > 7:
                for fidx in range(len(old_files)-7):
                    os.remove(old_dbs_dir + '/' + old_files[fidx])
            shutil.copy(dbname, backup)
            shutil.copy(tmp_db_name, dbname)
        else:
            print("  Dry run. No copy done.")
    else:
        print("No DB update.")

    conn = sqlite3.connect(dbname)
    cursor = conn.cursor()
    for _, f, _, c in existing_files:
        if c is not None:
            cursor.execute("update images set comment = '%s' where name = '%s'" % (c, f))
            print("Updating comment of image %s with '%s'..." % (f, c))
    if not dryrun:
        conn.commit()
    else:
        print("Dry run. Comment updates not commited.")
    conn.close()
    os.remove(tmp_db_name)

def doSync(args):
    synchronizeFromComputer(args.camera_dir, args.computer_dir + str(args.camera_id), args.dbname, args.dryrun, CAMERA_HOSTS[args.camera_id])

def doList(args):
    rows, dbname = getDataFromDB(args.computer_dir + str(args.camera_id), args.dbname)
    print("%4s %15s %22s %s" % ('id', 'name', 'date', 'comment'))
    print("%4s %15s %22s %s" % ('----', '-'*15, '-'*22, '-'*20))
    for i, f, d, c in rows:
        print("%4d %15s %22s %s" % (i, f, d, c))

def doDisplay(args):
    if args.filename is not None:
        fnames = [args.filename]
    else:
        fnames = getFilesById(args.id, args.img, args.computer_dir + str(args.camera_id), args.dbname)
    for f in fnames:
        img = loadCameraImage(f, args.band)
        if f.endswith('pkl'):
            frame, data = loadCameraRaw(f)
            print("Keys: %s" % data.keys())
            if args.webcam and 'webcam' in data:
                img = Image.fromarray(data['webcam'])
            elif args.viewfinder and 'viewFinder' in data:
                img = Image.fromarray(data['viewFinder'])
        img.show()

def doComment(args):
    rows, dbname = getDataFromDB(args.computer_dir + str(args.camera_id), args.dbname)
    conn = sqlite3.connect(dbname)
    cursor = conn.cursor()
    for i, _, _, c in rows:
        if i == args.id:
            if args.replace or c is None:
                c = args.comment
            else:
                c += '; ' + args.comment
            c = c.replace("'", '"')
            cursor.execute("update images set comment='%s' where id=%d" % (c, i))
            break
    if not args.dryrun:
        conn.commit()
    conn.close()

def doFile(args):
    if not os.path.exists(args.file):
        raise Exception("File %s not found. Nothing added." % args.file)
    comment = args.comment + ('; original file name: %s' % args.file)
    saveOutput(args.file, comment, args.camera_id)

def doClean(args):
    print("Syncing to local compute...")
    doSync(args)
    proc = subprocess.Popen(['ssh', CAMERA_HOSTS[args.camera_id], 'rm', '%s/IMG_?????.???' % args.camera_dir], stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
    proc.communicate()
    if proc.returncode != 0:
        print("Remove failed.")

def doDelete(args):
    rows, dbname = getDataFromDB(args.computer_dir + str(args.camera_id), args.dbname)
    for i, f, _, c in rows:
        if i == args.id:
            if c.find('SCAN:') >= 0 and c.find("DELETED:") < 0:
                print("Confirm you want to delete id %d with comment %s. y/N" % (i, c))
                ans = input()
                if len(ans) > 0 and ans.lower()[0] == 'y':
                    conn = sqlite3.connect(dbname)
                    cursor = conn.cursor()
                    fname = args.computer_dir + str(args.camera_id) +"/" + f
                    c = 'DELETED: ' + c
                    print("Removing %s and updating comment to %s..." % (fname, c))
                    if not args.dryrun and os.path.exists(fname):
                        os.remove(fname)
                        cursor.execute("update images set comment='%s' where id=%d" % (c, i))
                        conn.commit()
                    else:
                        print("  Aborted as dry run.")
                    conn.close()
                    return
                else:
                    print("Delete aborted.")
    print("No scan deleted for id %d" % args.id)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--camera_dir', help = 'Directory on the camera containing images. [%(default)s]', default = CAMERA_IMAGES_DIR)
    parser.add_argument('--computer_dir', help = 'Directory prefix on the compute to contain images. Gets camera id appended [%(default)s]', default = COMPUTER_IMAGES_DIR)
    parser.add_argument('--camera_id', help = 'Name or IP of camera host. [%(default)s]', default = 2, type = int)
    parser.add_argument('--dbname', help = 'Database of images. [%(default)s]', default = DATABASE_NAME)
    parser.add_argument('--dryrun', help = 'Does a dry run of synchronization. [%(default)s]', default = False, action = 'store_true')

    subparsers = parser.add_subparsers(help = 'Commmands for various DB activities')

    sp = subparsers.add_parser('sync', help = 'Synchronize camera to computer DB.')
    sp.set_defaults(func = doSync)

    sp = subparsers.add_parser('list', help = 'List database info.')
    sp.set_defaults(func = doList)

    sp = subparsers.add_parser('display', help = 'Display image in current database')
    sp.add_argument('--filename', help = 'Name of a pickle image to use for display.', default = None)
    sp.add_argument('--id', help = 'Id of image to display', type = int, default = None)
    sp.add_argument('--img', help = 'Substring in image name of image to display', type = str, default = None)
    sp.add_argument('--band', help = 'Band to display when showing raw data', type = int, default = 0)
    sp.add_argument('--webcam', help = 'Display the webcam image.', default = False, action = 'store_true')
    sp.add_argument('--viewfinder', help = 'Display the view finder image.', default = False, action = 'store_true')
    sp.set_defaults(func = doDisplay)

    sp = subparsers.add_parser('comment', help = 'Change the comment for a given id')
    sp.add_argument('id', help = 'Id of image to set comment', type = int)
    sp.add_argument('comment', help = 'Comment to replace')
    sp.add_argument('--replace', help = 'Replace the existing comment (default is to add onto existing comment)',
                    default = False, action = 'store_true')
    sp.set_defaults(func = doComment)

    sp = subparsers.add_parser('file', help = 'Add a file into the database for a given camera.')
    sp.add_argument('file', help = 'Name of file to add.')
    sp.add_argument('--comment', help = 'Comment to associate with file', default = 'External file added.')
    sp.set_defaults(func = doFile)

    sp = subparsers.add_parser('clean', help = 'Does a sync and then erases the images that are on the camera.')
    sp.set_defaults(func = doClean)

    sp = subparsers.add_parser('delete-scan', help = 'Deletes a scan. Only works on scan.')
    sp.add_argument('id', help = 'Id of image to set comment', type = int)
    sp.add_argument('--dryrun', help = 'Id of image to set comment. [%(default)s]', default = False, action = 'store_true')
    sp.set_defaults(func = doDelete)

    args = parser.parse_args()
    args.func(args)

