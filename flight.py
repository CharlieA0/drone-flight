# drone-flight  Copyright (C) 2020  Charles Vorbach
import setup_path
import airsim
from airsim import Vector3r, Pose, Quaternionr, YawMode

import sys 
import time 
import threading
import random 
import numpy as np
import pprint
import heapq
import pickle
import matplotlib.pyplot as plt
import cv2
import os
from collections import OrderedDict
from enum import Enum

from scipy.spatial.transform import Rotation as R

# Start up
client = airsim.MultirotorClient() 
client.confirmConnection() 
client.enableApiControl(True) 

# Operating Modes
class Task(Enum):
    TARGET = 1
    FOLLOWING = 2
    MAZE = 3
    HIKING = 4

TASK                  = Task.HIKING

# Constants
ENDPOINT_TOLERANCE     = 3.0       # m
TARGET_RADIUS          = 15        # m
MAZE_RADIUS            = 100       # m
MIN_BLAZE_STANCE       = 10        # m
PLOT_PERIOD            = 2.0       # s
PLOT_DELAY             = 1.0       # s
CONTROL_PERIOD         = 0.1       # s
SPEED                  = 0.5       # m/s
YAW_TIMEOUT            = 0.1       # s
VOXEL_SIZE             = 1.0       # m
CACHE_SIZE             = 100000    # number of entries
LOOK_AHEAD_DIST        = 1.0       # m
MAX_ENDPOINT_ATTEMPTS  = 5000
N_RUNS                 = 5000
ENABLE_PLOTTING        = False
ENABLE_RECORDING       = False
FLY_BY_MODEL           = False
LSTM_MODEL             = False
STARTING_WEIGHTS       = 'C:/Users/MIT Driverless/Documents/deepdrone/model-checkpoints/ncp-2020_11_15_21_42_12-weights.026--0.8673.hdf5'

CAMERA_FOV = np.pi / 8
RADIANS_2_DEGREES = 180 / np.pi
CAMERA_OFFSET = np.array([0.5, 0, -0.5])

ENDPOINT_OFFSET      = np.array([0, -0.03, 0.025])
DRONE_START          = np.array([-32295.757812, 2246.772705, 1894.547119])
WORLD_2_UNREAL_SCALE = 100

#TODO(cvorbach) CAMERA_HORIZONTAL_OFFSET

# Setup the network
SEQUENCE_LENGTH = 32
IMAGE_SHAPE     = (256,256,3)

model = None
if FLY_BY_MODEL:
    import tensorflow as tf
    from tensorflow import keras
    import kerasncp as kncp

    wiring = kncp.wirings.NCP(
        inter_neurons=12,   # Number of inter neurons
        command_neurons=8,  # Number of command neurons
        motor_neurons=3,    # Number of motor neurons
        sensory_fanout=4,   # How many outgoing synapses has each sensory neuron
        inter_fanout=4,     # How many outgoing synapses has each inter neuron
        recurrent_command_synapses=4,   # Now many recurrent synapses are in the
                                        # command neuron layer
        motor_fanin=6,      # How many incomming syanpses has each motor neuron
    )

    rnnCell = kncp.LTCCell(wiring)

    ncpModel = keras.models.Sequential()
    ncpModel.add(keras.Input(shape=(None, *IMAGE_SHAPE)))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Conv2D(filters=24, kernel_size=(5,5), strides=(2,2), activation='relu')))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Conv2D(filters=36, kernel_size=(5,5), strides=(2,2), activation='relu')))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Conv2D(filters=48, kernel_size=(3,3), strides=(2,2), activation='relu')))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Conv2D(filters=64, kernel_size=(3,3), strides=(1,1), activation='relu')))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Conv2D(filters=64, kernel_size=(3,3), strides=(1,1), activation='relu')))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Flatten()))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dropout(rate=0.5)))
    ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dense(units=24, activation='linear')))
    # ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dropout(rate=0.5)))
    # ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dense(units=100,  activation='relu')))
    # ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dropout(rate=0.3)))
    # ncpModel.add(keras.layers.TimeDistributed(keras.layers.Dense(units=24,   activation='relu')))
    ncpModel.add(keras.layers.RNN(rnnCell, return_sequences=True))

    ncpModel.compile(
        optimizer=keras.optimizers.Adam(0.00005), loss="cosine_similarity",
    )

    # LSTM network
    penultimateOutput = ncpModel.layers[-2].output
    lstmLayer1        = keras.layers.SimpleRNN(units=64, return_sequences=True, activation='relu')(penultimateOutput)
    lstmLayer2        = keras.layers.SimpleRNN(units=3, return_sequences=True, activation='relu')(lstmLayer1)
    lstmModel = keras.models.Model(ncpModel.input, lstmLayer2)

    # Configure the model we will train
    if LSTM_MODEL:
        flightModel = lstmModel
    else:
        flightModel = ncpModel

    # Load weights
    flightModel.load_weights(STARTING_WEIGHTS)

def normalize(vector):
    if np.linalg.norm(vector) == 0:
        raise ZeroDivisionError()
    return vector / np.linalg.norm(vector)


class CubicSpline:
    def __init__(self, x, y, tol=1e-10):
        self.x = x
        self.y = y
        self.coeff = self.fit(x, y, tol)

    def fit(self, x, y, tol=1e-10):
        """
        Interpolate using natural cubic splines.
    
        Generates a strictly diagonal dominant matrix then solves.
    
        Returns coefficients:
        b, coefficient of x of degree 1
        c, coefficient of x of degree 2
        d, coefficient of x of degree 3
        """ 
    
        x = np.array(x)
        y = np.array(y)
    
        # check if sorted
        if np.any(np.diff(x) < 0):
            idx = np.argsort(x)
            x = x[idx]
            y = y[idx]

        size = len(x)
        delta_x = np.diff(x)
        delta_y = np.diff(y)
    
        # Initialize to solve Ac = k
        A = np.zeros(shape = (size,size))
        k = np.zeros(shape=(size,1))
        A[0,0] = 1
        A[-1,-1] = 1
    
        for i in range(1,size-1):
            A[i, i-1] = delta_x[i-1]
            A[i, i+1] = delta_x[i]
            A[i,i] = 2*(delta_x[i-1]+delta_x[i])

            k[i,0] = 3*(delta_y[i]/delta_x[i] - delta_y[i-1]/delta_x[i-1])
    
        # Solves for c in Ac = k
        c = np.linalg.solve(A, k)
    
        # Solves for d and b
        d = np.zeros(shape = (size-1,1))
        b = np.zeros(shape = (size-1,1))
        for i in range(0,len(d)):
            d[i] = (c[i+1] - c[i]) / (3*delta_x[i])
            b[i] = (delta_y[i]/delta_x[i]) - (delta_x[i]/3)*(2*c[i] + c[i+1])    
    
        return b.squeeze(), c.squeeze(), d.squeeze()

    def __call__(self, t):
        '''
        Returns the value of the spline at t in [x[0], x[-1]]
        '''

        x = self.x
        y = self.y
        b, c, d = self.coeff

        if t < x[0] or t > x[-1]:
            raise Exception("Can't extrapolate")

        # Index of segment to use
        idx = np.argmax(x > t) - 1
                
        dx = t - x[idx]
        value = y[idx] + b[idx]*dx + c[idx]*dx**2 + d[idx]*dx**3
        return value

class Path:
    def __init__(self, knotPoints):
        self.knotPoints = knotPoints
        self.fit(knotPoints)

    def fit(self, knotPoints):
        knots = np.array(knotPoints)
        t = np.linspace(0, 1, knots.shape[0])
        self.xSpline = CubicSpline(t, knots[:, 0])
        self.ySpline = CubicSpline(t, knots[:, 1])
        self.zSpline = CubicSpline(t, knots[:, 2])

    def __call__(self, t):
        return np.array([
            self.xSpline(t),
            self.ySpline(t),
            self.zSpline(t)
        ])

    def project(self, point):
        position, _ = getPose()
        tSamples = np.linspace(0, 1, num=1000)
        nearstT  = tSamples[np.argmin([np.linalg.norm(position - self(t)) for t in tSamples])]
        return nearstT
    
    def end(self):
        return self.knotPoints[-1]


def randomWalk(start, momentum=0.75, stepSize=0.75, gradientLimit=np.pi/9, zLimit=(-20, 0), pathLength=30, occupancyMap=None, retryLimit = 10):
    normalDistribution = np.random.default_rng().normal 
    stepDirection = normalize(np.array([normalDistribution(), normalDistribution(), normalDistribution()]))
    path = [start]

    for i in range(retryLimit):
        for _ in range(pathLength):

            # Generate an unoccupied next step in the random walk
            isUnoccupiedNextStep = False
            stuckSteps = 0
            while not isUnoccupiedNextStep:
                rotation = R.from_matrix([[stepDirection[0], 0, 0], [0, stepDirection[1], 0], [0, 0, stepDirection[2]]])
                perturbance = rotation.apply(normalize(np.array((0, normalDistribution(), normalDistribution()))))

                # the continue in previous direction with a random perturbance left/right and up/down
                stepDirection = normalize(momentum * stepDirection + (1 - momentum) * perturbance)

                # # apply gradient limit
                stepDirection = np.array([min(max((u, -gradientLimit)), gradientLimit) for u in stepDirection])

                nextStep = path[-1] + stepSize * stepDirection

                # # apply altitude limits
                nextStep[2] = min(max(nextStep[2], zLimit[0]), zLimit[1])

                isUnoccupiedNextStep = occupancyMap is None or nextStep not in occupancyMap 

                stuckSteps += int(not isUnoccupiedNextStep)
                if stuckSteps > retryLimit:
                    break 

            path.append(nextStep)

        if stuckSteps < retryLimit:
            break

    if i > retryLimit:
        raise Exception('Couldn\'t find free random walk')

    return path

# Utilities
class LRUCache:
    def __init__(self, capacity: int):
        self.cache = OrderedDict()
        self.capacity = capacity

    def __contains__(self, key):
        if key in self.cache:
            self.cache.move_to_end(key) # Move to front of LRU cache
            return True
        return False

    def add(self, key):
        self.cache[key] = None          # Don't care about the dict's value, just its set of keys
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def discard(self, key):
        self.cache.pop(key, None)

    def keys(self):
        return self.cache.keys()

class VoxelOccupancyCache:

    def __init__(self, voxelSize: float, capacity: int):
        self.voxelSize  = voxelSize
        self.cache      = LRUCache(capacity)
    
    def addPoint(self, point):
        voxel = self.point2Voxel(point)

        self.cache.add(voxel)
        for v in self.getAdjacentVoxels(voxel):
            self.cache.add(v)

    def __contains__(self, point):
        voxel = self.point2Voxel(point)
        return voxel in self.cache

    def point2Voxel(self, point):
        return tuple(self.voxelSize * int(round(v / self.voxelSize)) for v in point)

    def getAdjacentVoxels(self, voxel):
        adjacentVoxels = [
            (voxel[0] - 1, voxel[1] - 1, voxel[2] - 1),
            (voxel[0],     voxel[1] - 1, voxel[2] - 1),
            (voxel[0] + 1, voxel[1] - 1, voxel[2] - 1),

            (voxel[0] - 1, voxel[1], voxel[2] - 1),
            (voxel[0],     voxel[1], voxel[2] - 1),
            (voxel[0] + 1, voxel[1], voxel[2] - 1),

            (voxel[0] - 1, voxel[1] + 1, voxel[2] - 1),
            (voxel[0],     voxel[1] + 1, voxel[2] - 1),
            (voxel[0] + 1, voxel[1] + 1, voxel[2] - 1),

            (voxel[0] - 1, voxel[1] - 1, voxel[2]),
            (voxel[0],     voxel[1] - 1, voxel[2]),
            (voxel[0] + 1, voxel[1] - 1, voxel[2]),

            (voxel[0] - 1, voxel[1], voxel[2]),
            (voxel[0],     voxel[1], voxel[2]),
            (voxel[0] + 1, voxel[1], voxel[2]),

            (voxel[0] - 1, voxel[1] + 1, voxel[2]),
            (voxel[0],     voxel[1] + 1, voxel[2]),
            (voxel[0] + 1, voxel[1] + 1, voxel[2]),

            (voxel[0] - 1, voxel[1] - 1, voxel[2] + 1),
            (voxel[0],     voxel[1] - 1, voxel[2] + 1),
            (voxel[0] + 1, voxel[1] - 1, voxel[2] + 1),

            (voxel[0] - 1, voxel[1], voxel[2] + 1),
            (voxel[0],     voxel[1], voxel[2] + 1),
            (voxel[0] + 1, voxel[1], voxel[2] + 1),

            (voxel[0] - 1, voxel[1] + 1, voxel[2] + 1),
            (voxel[0],     voxel[1] + 1, voxel[2] + 1),
            (voxel[0] + 1, voxel[1] + 1, voxel[2] + 1),
        ]

        return adjacentVoxels

    def getEmptyNeighbors(self, voxel):
        neighbors = []
        possibleNeighbors = self.getAdjacentVoxels(voxel)

        for v in possibleNeighbors:
            if v not in self.cache:
                neighbors.append(v) 

        return neighbors

    def plotOccupancies(self):
        occupiedPoints = [Vector3r(float(v[0]), float(v[1]), float(v[2])) for v in self.cache.keys()]
        client.simPlotPoints(occupiedPoints, color_rgba = [0.0, 0.0, 1.0, 1.0], duration=PLOT_PERIOD-PLOT_DELAY) 


def world2UnrealCoordinates(vector):
    return (vector + DRONE_START) * WORLD_2_UNREAL_SCALE


def unreal2WorldCoordinates(vector):
    return (vector - DRONE_START) / WORLD_2_UNREAL_SCALE


def isVisible(point, position, orientation):
    # Edge case point == position
    if np.linalg.norm(point - position) < 0.05:
        return True

    # Check if endpoint is in frustrum
    xUnit = np.array([1, 0, 0])
    cameraDirection = R.from_quat(orientation).apply(xUnit)

    endpointDirection = point - position
    endpointDirection /= np.linalg.norm(endpointDirection)

    # TODO(cvorbach) check square, not circle
    angle = np.arccos(np.dot(cameraDirection, endpointDirection)) 

    if abs(angle) > CAMERA_FOV:
        return False

    # TODO(cvorbach) Check for occlusions with ray-tracing

    return True


def orientationAt(endpoint, position):
    # Get the drone orientation that faces towards the endpoint at position
    displacement = np.array(endpoint) - np.array(position)
    endpointYaw = np.arctan2(displacement[1], displacement[0])
    orientation = R.from_euler('xyz', [0, 0, endpointYaw]).as_quat()

    return orientation

def euclidean(voxel1, voxel2):
    return np.linalg.norm(np.array(voxel1) - np.array(voxel2))

def greedy(voxel1, voxel2):
    return 100*euclidean(voxel1, voxel2)

# A* Path finding 
def findPath(startpoint, endpoint, map, h=greedy, d=euclidean):
    start = map.point2Voxel(startpoint)
    end   = map.point2Voxel(endpoint)

    cameFrom = dict()

    gScore = dict()
    gScore[start] = 0

    fScore = dict()
    fScore[start] = h(start, endpoint)

    openSet = [(fScore[start], start)]

    while openSet:
        current = heapq.heappop(openSet)[1]

        if current == end:
            path = [current]
            while path[-1] != start:
                current = cameFrom[current]
                path.append(current)
            
            return list(reversed(path))

        for neighbor in map.getEmptyNeighbors(current):
            # skip neighbors from which the endpoint isn't visible
            neighborOrientation = orientationAt(endpoint, neighbor)
            if not isVisible(np.array(end), np.array(neighbor), neighborOrientation):
                continue

            tentativeGScore = gScore.get(current, float("inf")) + d(current, neighbor)

            if tentativeGScore < gScore.get(neighbor, float('inf')):
                cameFrom[neighbor] = current
                gScore[neighbor]   = tentativeGScore

                if neighbor in fScore:
                    try:
                        openSet.remove((fScore[neighbor], neighbor))
                    except:
                        pass
                fScore[neighbor]   = gScore.get(neighbor, float('inf')) + h(neighbor, endpoint)

                heapq.heappush(openSet, (fScore[neighbor], neighbor))
        
    raise ValueError("Couldn't find a path")


def getTime():
    return 1e-9 * client.getMultirotorState().timestamp


def getPose():
    pose        = client.simGetVehiclePose()
    position    = pose.position.to_numpy_array() - CAMERA_OFFSET
    orientation = pose.orientation.to_numpy_array()
    return position, orientation


def isValidEndpoint(endpoint, map):
    if endpoint in map:
        return False

    position, orientation = getPose()
    if not isVisible(endpoint, position, orientation):
        return False

    # TODO(cvorbach) Check there is a valid path

    return True


def generateTarget(occupancyMap, radius=10):
    isValid = False
    attempts = 0
    while not isValid:
        # TODO(cvorbach) smarter generation without creating points under terrain
        position, orientation = getPose()
        yawRotation = R.from_euler('xyz', [0, 0, R.from_quat(orientation).as_euler('xyz')[2]])

        endpoint = position + yawRotation.apply(occupancyMap.point2Voxel(radius * np.array([random.random(), 0.1*random.random(), -random.random()])))
        endpoint = occupancyMap.point2Voxel(endpoint)
        isValid = isValidEndpoint(endpoint, occupancyMap)

        attempts += 1
        if attempts > MAX_ENDPOINT_ATTEMPTS:
            return None

    return np.array(endpoint)


def turnTowardEndpoint(endpoint, timeout=0.01):
    position, _ = getPose()
    displacement = endpoint - position

    endpointYaw = np.arctan2(displacement[1], displacement[0]) * RADIANS_2_DEGREES
    client.rotateToYawAsync(endpointYaw, timeout_sec = timeout).join()
    print("Turned toward endpoint")


def tryPlotting(lastPlotTime, occupancyMap):
    if getTime() < PLOT_PERIOD + lastPlotTime:
        return lastPlotTime

    # occupancyMap.plotOccupancies()

    print("Replotted :)")
    return getTime()


def updateOccupancies(map):
    lidarData = client.getLidarData()
    lidarPoints = np.array(lidarData.point_cloud, dtype=np.dtype('f4'))
    if len(lidarPoints) >=3:
        lidarPoints = np.reshape(lidarPoints, (lidarPoints.shape[0] // 3, 3))

        for p in lidarPoints:
            map.addPoint(p)

    # print("Lidar data added")


def getNearestPoint(trajectory, position):
    closestDist = None
    for i in range(len(trajectory)):
        point = trajectory[i]
        dist  = np.linalg.norm(point - position)

        if closestDist is None or dist < closestDist:
            closestDist = dist
            closestIdx  = i

    return closestIdx

def getProgress(trajectory, currentIdx, position):
    if len(trajectory) <= 1:
        raise Exception("Trajectory is too short")

    progress = 0
    for i in range(len(trajectory) - 1):
        p1 = trajectory[i]
        p2 = trajectory[i+1]

        if i < currentIdx:
            progress += np.linalg.norm(p2 - p1)
        else:
            partialProgress = (position - p1).dot(p2 - p1) / np.linalg.norm(p2 - p1)
            progress += partialProgress
            break

    return progress

def pursuitVelocity(trajectory):
    '''
    This function is kinda of a mess, but basically it implements 
    carrot following of a lookahead.

    The three steps are:
    1. Find the point on the path nearest to the drone as start of carrot
    2. Find points in front of and behind the look ahead point by arc length
    3. Linearly interpolate to find the lookahead point and chase it.
    '''

    position, _ = getPose()
    startIdx = getNearestPoint(trajectory, position)
    progress = getProgress(trajectory, startIdx, position)

    arcLength   = -progress
    pointBehind = trajectory[0]
    for i in range(1, len(trajectory)):
        pointAhead = trajectory[i]

        arcLength += np.linalg.norm(pointAhead - pointBehind)
        if arcLength > LOOK_AHEAD_DIST:
            break

        pointBehind = pointAhead

    # if look ahead is past the end of the trajectory
    if np.array_equal(pointAhead, pointBehind): 
        lookAheadPoint = pointAhead

    else:
        behindWeight = (arcLength - LOOK_AHEAD_DIST) / np.linalg.norm(pointAhead - pointBehind)
        aheadWeight = 1.0 - behindWeight

        # sanity check
        if not (0 <= aheadWeight <= 1 or 0 <= behindWeight <= 1):
            raise Exception("Invalid Interpolation Weights")

        lookAheadPoint = aheadWeight * pointAhead + behindWeight * pointBehind

    # Compute velocity to pursue lookahead point
    pursuitVector = lookAheadPoint - position
    pursuitVector = SPEED * pursuitVector / np.linalg.norm(pursuitVector)

    return pursuitVector

def followPath(path, lookAhead = 2, dt = 1e-4, marker=None, earlyStopDistance=None, planningWrapper=None, planningKnots=None):
    t = 0
    lookAheadPoint  = path(0)
    reachedEnd      = False

    lastPlotTime    = getTime()
    markerPose      = airsim.Pose()

    planningThread  = None
    controlThread   = None

    print('started following path')

    if ENABLE_PLOTTING:
        client.simPlotPoints([Vector3r(*path(t)) for t in np.linspace(0, 1, 1000)], duration = 60)

    if ENABLE_RECORDING:
        client.startRecording()

    # control loop
    while not reachedEnd:
        position, _ = getPose()
        displacement = lookAheadPoint - position
        updateOccupancies(occupancyMap)

        # handle planning thread if needed
        if planningWrapper is not None:

            # if we have finished planning
            if planningThread is None or not planningThread.is_alive():

                # update the spline path
                if np.any(planningKnots != path.knotPoints):
                    path.fit(planningKnots.copy())
                    t = path.project(position) # find the new nearest path(t)

                # restart planning
                planningThread = threading.Thread(target=planningWrapper, args=(planningKnots,))
                planningThread.start()

            planningThread.join(timeout=CONTROL_PERIOD)

        # advance the pursuit point if needed
        while np.linalg.norm(displacement) < lookAhead:
            t += dt
            if t < 1: 
                lookAheadPoint = path(t)
                displacement = lookAheadPoint - position
            else:
                reachedEnd = True
                break

        # optionally stop within distance of path end
        if earlyStopDistance is not None:
            if np.linalg.norm(path(1.0) - position) < earlyStopDistance:
                reachedEnd = True

        if reachedEnd:
            break

        # place marker if passed
        if marker is not None:
            markerPose.position = Vector3r(*lookAheadPoint)
            client.simSetObjectPose(marker, markerPose)

        # get yaw angle and pursuit velocity to the lookAheadPoint
        yawAngle = np.arctan2(displacement[1], displacement[0]) * RADIANS_2_DEGREES
        velocity = SPEED * normalize(displacement)

        # plot
        if ENABLE_PLOTTING:
            lastPlotTime = tryPlotting(lastPlotTime, occupancyMap)

        # start control thread
        if controlThread is not None:
            controlThread.join()
        controlThread = client.moveByVelocityAsync(float(velocity[0]), float(velocity[1]), float(velocity[2]), CONTROL_PERIOD, yaw_mode=YawMode(is_rate = False, yaw_or_rate = yawAngle))

    # hide the marker
    if marker is not None:
        markerPose.position = Vector3r(0,0,0)
        client.simSetObjectPose(marker, markerPose)

    if ENABLE_RECORDING:
        client.startRecording()


def moveToEndpoint(endpoint, occupancyMap, model = None):
    updateOccupancies(occupancyMap)

    position, _  = getPose()
    pathKnots    = findPath(position, endpoint, occupancyMap)
    pathToEndpoint = Path(pathKnots.copy())

    def planningWrapper(knots):
        # run planning
        newKnots = findPath(position, endpoint, occupancyMap)

        # replace the old knots
        knots.clear()
        for k in newKnots:
            knots.append(k)
            
        # print('Finished Planning')

    followPath(pathToEndpoint, earlyStopDistance=ENDPOINT_TOLERANCE, planningWrapper=planningWrapper, planningKnots=pathKnots)
    print('Reached Endpoint')


def checkBand(k, blazes, blazeStart, zLimit):

    band = []

    # check sides of each square of radius k between zLimits
    for z in reversed(range(zLimit[0], zLimit[1])):
        corners = np.array([
            (blazeStart[0] - k, blazeStart[1] - k, z),
            (blazeStart[0] + k, blazeStart[1] - k, z),
            (blazeStart[0] + k, blazeStart[1] + k, z),
            (blazeStart[0] - k, blazeStart[1] + k, z)
        ])

        tangentVectors = np.array([
            (1, 0, 0),
            (0, 1, 0),
            (-1, 0, 0),
            (0, -1, 0)
        ])

        sideLength = 2*k-1

        # check each side
        for corner, tangent in zip(corners, tangentVectors):
            for i in range(sideLength):
                voxel = occupancyMap.point2Voxel(corner + i * tangent)
                band.append(voxel)

                if voxel in occupancyMap and not np.any([np.linalg.norm(np.array(b) - voxel) < MIN_BLAZE_DISTANCE for b in blazes]):
                    return voxel

    # client.simPlotPoints([Vector3r(*v) for v in band])
    return None


def generateHikingBlazes(start, occupancyMap, numBlazes = 2, zLimit=(-10, -5), maxSearchDepth=1000):
    blazes = []
    blazeStart = occupancyMap.point2Voxel(start)

    # lastPlotTime = tryPlotting(-float('inf'), occupancyMap)

    for _ in range(numBlazes):
        nextBlaze   = None
        
        k = 5
        while nextBlaze is None and k < maxSearchDepth: 
            nextBlaze = checkBand(k, blazes, blazeStart, zLimit)
            k += 1

        if k == maxSearchDepth:
            raise Exception('Could not find a tree to blaze')

        blazes.append(nextBlaze)
        blazeStart = blazes[-1]

    return blazes



# -----------------------------
# MAIN
# -----------------------------

# Takeoff
client.armDisarm(True)
# client.takeoffAsync().join()
# print("Taken off")

occupancyMap = VoxelOccupancyCache(VOXEL_SIZE, CACHE_SIZE)

# get the markers
markers = client.simListSceneObjects('Red_Cube.*') 

if len(markers) < 1:
    raise Exception('Didn\'t find any endpoint markers. Check there is a Red_Cube is in the scene')

# start the markers out of the way
markerPose = airsim.Pose()
markerPose.position = Vector3r(0, 0, 100)
for marker in markers:
    client.simSetObjectPose(marker, markerPose)
 
# Collect data runs
for i in range(N_RUNS):
    position, orientation = getPose()

    if TASK == Task.TARGET:
        marker = markers[0]

        # Random rotation
        client.rotateToYawAsync(random.random() * 2.0 * np.pi * RADIANS_2_DEGREES).join()

        # Set up
        endpoint = generateTarget(occupancyMap, radius=TARGET_RADIUS)
        if endpoint is None:
            continue
            
        # place endpoint marker
        endpointPose = airsim.Pose()
        endpointPose.position = Vector3r(*endpoint)
        client.simSetObjectPose(marker, endpointPose)

        turnTowardEndpoint(endpoint, timeout=10)

        moveToEndpoint(endpoint, occupancyMap)

    if TASK == Task.FOLLOWING:

        marker = markers[0]

        updateOccupancies(occupancyMap)
        print('updated occupancies')

        path = Path(randomWalk(position, stepSize=0.5, occupancyMap=occupancyMap))
        print('got path')

        followPath(path, marker=marker)
        print('reached path end')

    if TASK == Task.HIKING:
        print('move to z-level')
        zLimit = (-5, 0)
        client.moveToZAsync((zLimit[0] + zLimit[1])/2, 1).join()
        print('reached z-level')

        updateOccupancies(occupancyMap)
        print('updated occupancies')

        print('getting blazes')
        hikingBlazes = generateHikingBlazes(position, occupancyMap, zLimit=zLimit)
        print('Got blazes')

        if len(hikingBlazes) > len(markers):
            raise Exception('Not enough markers for each blaze to get one')

        # place makers
        markerPose = airsim.Pose()
        for i, blaze in enumerate(hikingBlazes):
            markerPose.position = Vector3r(*blaze)
            client.simSetObjectPose(markers[i], markerPose)

        for blaze in hikingBlazes:
            moveToEndpoint(blaze, occupancyMap)

    if TASK == Task.MAZE:
        # TODO(cvorbach) reimplement me
        # record the direction vector
        marker = markers[0]

        # Set up
        endpoint = generateTarget(occupancyMap, radius=MAZE_RADIUS)
        if endpoint is None:
            continue
            
        # place endpoint marker
        endpointPose = airsim.Pose()
        endpointPose.position = Vector3r(*endpoint)
        client.simSetObjectPose(marker, endpointPose)

        turnTowardEndpoint(endpoint, timeout=10)

        moveToEndpoint(endpoint, occupancyMap)


print('Finished Data Runs')