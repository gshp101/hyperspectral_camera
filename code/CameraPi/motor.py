import RPi.GPIO as GPIO
import time
import sys

class Motor(object):
    GREEN_ACTIVE = 1
    WHITE_ACTIVE = 2
    DISABLE = 4

    CLOCKWISE = GPIO.LOW
    COUNTER_CLOCKWISE = GPIO.HIGH


    def __init__(self, dirPin = 26, stepPin = 20, sleepPin = 16, speedPin = 19, whiteSwitch = 13, greenSwitch = 6, range = 160, disableSwitches = False):
        self.dirPin = dirPin
        self.sleepPin = sleepPin
        self.stepPin = stepPin
        self.speedPin = speedPin
        self.greenSwitch = greenSwitch
        self.whiteSwitch = whiteSwitch
        self.range = range
        self.disableSwitches = disableSwitches
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.sleepPin, GPIO.OUT)
        GPIO.setup(self.speedPin, GPIO.OUT)
        GPIO.setup(self.stepPin, GPIO.OUT)
        GPIO.setup(self.dirPin, GPIO.OUT)
        GPIO.setup(self.greenSwitch, GPIO.IN)
        GPIO.setup(self.whiteSwitch, GPIO.IN)
        self.disableFlag = 0
        self.currentAngle = 0
        self.clockwise()
        self.fast()
        self.isCalibrated = False
        self.sleep()
        self._checkSwitches()

    def _setSwitch(self):
        if self.disableSwitches:
            sys.stdout.write("MOTOR: Switches have been disabled. Illegal call to _setSwitch.\n")
            return False
        if self.currentDirectionIsClockwise:
            expectedSwitch = 'right'
            changeDir = self.counterClockwise
        else:
            expectedSwitch = 'left'
            changeDir = self.clockwise

        if self.disableFlag & (self.WHITE_ACTIVE | self.GREEN_ACTIVE) == (self.WHITE_ACTIVE | self.GREEN_ACTIVE):
            self.disable()
            sys.stdout.write("MOTOR: Both switches are active. there's some fault with the hardware. Disabling the motor.\n")
            return False
        pin = self.whiteSwitch if self.disableFlag & self.WHITE_ACTIVE else self.greenSwitch
        self.switches[expectedSwitch] = pin
        changeDir()
        for i in range(100):
            time.sleep(0.01)
            self.clearSwitchFlags()
            if self.step(0.01):
                if not self.disableFlag:
                    return True
                self.clearSwitchFlags()

        sys.stdout.write("MOTOR: Failed to clear switch. Disabling.")
        self.disable()
        return False

    def calibrate(self, force = False):
        if self.isCalibrated and not force:
            return True

        if self.disableSwitches:
            sys.stdout.write("Switches have been disabled. Cannot calibrate.")
            return False

        self.enable()
        self.sleep()
        time.sleep(1)
        self.clearSwitchFlags()
        self._checkSwitches()
        if self.disableFlag &(self.GREEN_ACTIVE | self.WHITE_ACTIVE):
            sys.stdout.write("CALIBRATION ERROR: Starting with switch active. Position camera away from switch.\n")
            return False
        self.wake()
        self.slow()
        self.clockwise()
        RevsPerSec = 16
        StepsPerRev = 360/self.degreesPerStep
        SecsPerStep = max([1/(RevsPerSec*StepsPerRev), 0.01])
        self.switches = { 'right': None, 'left': None }
        maxSteps = int(self.range*1.1/self.degreesPerStep) + 1
        self.step()

        count = maxSteps
        while count > 0:
            if not self.step(SecsPerStep):
                break
            count -= 1
        if self.disableFlag == 0:
            sys.stdout.write("MOTOR: Failed to find right switch. There is a hardware fault. Disabling.\n")
            self.disable()
            return False
        if not self._setSwitch(): return
        self.counterClockwise()
        time.sleep(0.5)
        count = 0
        while count < maxSteps:
            if not self.step(SecsPerStep):
                break
            count += 1
        if self.disableFlag == 0:
            sys.stdout.write("MOTOR: Failed to find left switch. There is a hardware fault. Disabling.\n")
            self.disable()
            return False
        if not self._setSwitch(): return
        self.clockwise()
        time.sleep(0.5)
        for i in range(count // 2):
            if not self.step(SecsPerStep):
                sys.stdout.write("MOTOR: Switch unexpectedly reached. Hardware fault. Disabling.\n")
                self.disable()
                return False

        if self.switches['left'] is None or self.switches['right'] is None or self.switches['left'] == self.switches['right']:
            sys.stdout.write("MOTOR: Did not find left and right switches. Disabling.\n")
            self.disable()
            return False
        self.currentAngle = 0
        self.isCalibrated = True
        return True

    def _checkSwitches(self):
        if self.disableSwitches:
            return
        if GPIO.input(self.greenSwitch):
            self.disableFlag |= self.GREEN_ACTIVE
            self.isCalibrated = False
        if GPIO.input(self.whiteSwitch):
            self.disableFlag |= self.WHITE_ACTIVE
            self.isCalibrated = False

    def slow(self):
        self.degreesPerStep = 1.8/16
        self._checkSwitches()
        GPIO.output(self.speedPin, GPIO.HIGH)

    def fast(self):
        self.degreesPerStep = 1.8
        self._checkSwitches()
        GPIO.output(self.speedPin, GPIO.LOW)

    def hires(self):
        self.slow()

    def lores(self):
        self.fast()

    def sleep(self):
        self.sleeping = True
        self.isCalibrated = False
        self._checkSwitches()
        GPIO.output(self.sleepPin, GPIO.LOW)

    def wake(self):
        self.sleeping = False
        self._checkSwitches()
        GPIO.output(self.sleepPin, GPIO.HIGH)

    def status(self):
        self._checkSwitches()
        status = 'sleeping' if self.sleeping else ''
        status += ' disabled' if self.disableFlag & self.DISABLE else ''
        status += ' green on' if self.disableFlag & self.GREEN_ACTIVE else ''
        status += ' white on' if self.disableFlag & self.WHITE_ACTIVE else ''
        status += ' %0.2f degrees per step' % self.degreesPerStep
        status += ' clockwise step' if self.currentDirectionIsClockwise else ' counter clockwise step'
        return status

    def __repr__(self):
        return ("Motor: " + self.status())

    def clockwise(self):
        self._checkSwitches()
        self.currentDirectionIsClockwise = True
        GPIO.output(self.dirPin, self.CLOCKWISE)

    def counterClockwise(self):
        self._checkSwitches()
        self.currentDirectionIsClockwise = False
        GPIO.output(self.dirPin, self.COUNTER_CLOCKWISE)

    def testSwitches(self):
        self.enable()
        self.sleep()
        def doTest(self, name):
            print("You have 5 seconds to activate a %s switch." % name)
            self.clearSwitchFlags()
            tm = 0
            delta = 0.1
            while tm < 5 and not self.disableFlag:
                self._checkSwitches()
                time.sleep(delta)
                tm += delta

            if not self.disableFlag:
                print("FAILURE: No switch was detected.")
            elif self.disableFlag & self.WHITE_ACTIVE:
                print("SUCCESS: 'White' switch activated.")
                return True
            elif self.disableFlag & self.GREEN_ACTIVE:
                print("SUCCESS: 'Green' switch activated.")
                return True
            else:
                print("FAILURE: Unknown condition detected.")
            return False

        if not doTest(self, 'single'):
            return False
        flag = self.disableFlag

        print("You have 5 seconds to release switch.")
        time.sleep(5)
        if not doTest(self, 'other'):
            return False
        flag |= self.disableFlag
        self.clearSwitchFlags()

        if flag != self.WHITE_ACTIVE | self.GREEN_ACTIVE:
            print ("FAILURE: Both switches not tested.")
            return False

        print("SUCCESS: Both switches tested succesfully.")
        return True

    def clearSwitchFlags(self):
        self.disableFlag &= 0xffffffff ^ self.GREEN_ACTIVE ^ self.WHITE_ACTIVE

    def disable(self):
        self.sleep()
        self.disableFlag |= self.DISABLE

    def enable(self):
        self.disableFlag &= 0xffffffff ^ self.DISABLE

    def step(self, tm = .01, safety = True):
        if self.disableFlag and safety:
            return False
        GPIO.output(self.stepPin, GPIO.HIGH)
        time.sleep(tm/2)
        GPIO.output(self.stepPin, GPIO.LOW)
        time.sleep(tm/2)
        self.currentAngle += self.degreesPerStep if self.currentDirectionIsClockwise else -self.degreesPerStep
        self._checkSwitches()
        return True

    def rotate(self, angle, tm = 0.01):
        if angle > self.currentAngle:
            self.clockwise()
            angle = angle - self.currentAngle
        else:
            self.counterClockwise()
            angle = self.currentAngle - angle
        while angle > 0:
            if not self.step(tm):
                print("Rotation hit stop.")
                return False
            angle -= self.degreesPerStep
        return True