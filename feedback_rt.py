# This code should be run in console room computer to display the feedback morphings
from __future__ import print_function, division
#os.chdir("/Volumes/GoogleDrive/My Drive/Turk_Browne_Lab/rtSynth_repo/kp_scratch/expcode")
from psychopy import visual, event, core, logging, gui, data, monitors
from psychopy.hardware.emulator import launchScan, SyncGenerator
from PIL import Image
import string
import fmrisim as sim
import numpy as np
import pandas as pd
import sys
import os
import pylink
from tqdm import tqdm
import time
import re


import os
import sys
import time
import json
import re
import argparse
import logging
import threading
import websocket
from queue import Queue, Empty
# import project modules
# Add base project path (two directories up)
currPath = os.path.dirname(os.path.realpath(__file__))
rootPath = os.path.dirname(currPath)
sys.path.append(rootPath)
from rtCommon.utils import DebugLevels, installLoggers
from rtCommon.projectUtils import login, certFile, checkSSLCertAltName, makeSSLCertFile


class WsFeedbackReceiver:
    ''' A client that receives classification results (feedback for the subject)
        from the cloud service. The communication connection
        is made with webSockets (ws)
    '''
    serverAddr = None
    sessionCookie = None
    needLogin = True
    shouldExit = False
    validationError = None
    recvThread = None
    msgQueue = Queue()
    # Synchronizing across threads
    clientLock = threading.Lock()

    @staticmethod
    def startReceiverThread(serverAddr, retryInterval=10,
                            username=None, password=None,
                            testMode=False):
        WsFeedbackReceiver.recvThread = \
            threading.Thread(name='recvThread',
                             target=WsFeedbackReceiver.runReceiver,
                             args=(serverAddr,),
                             kwargs={'retryInterval': retryInterval,
                                     'username': username,
                                     'password': password,
                                     'testMode': testMode})
        WsFeedbackReceiver.recvThread.setDaemon(True)
        WsFeedbackReceiver.recvThread.start()

    @staticmethod
    def runReceiver(serverAddr, retryInterval=10,
                    username=None, password=None,
                    testMode=False):
        WsFeedbackReceiver.serverAddr = serverAddr
        # go into loop trying to do webSocket connection periodically
        WsFeedbackReceiver.shouldExit = False
        while not WsFeedbackReceiver.shouldExit:
            try:
                if WsFeedbackReceiver.needLogin or WsFeedbackReceiver.sessionCookie is None:
                    WsFeedbackReceiver.sessionCookie = login(serverAddr, username, password, testMode=testMode)
                wsAddr = os.path.join('wss://', serverAddr, 'wsSubject')
                if testMode:
                    print("Warning: using non-encrypted connection for test mode")
                    wsAddr = os.path.join('ws://', serverAddr, 'wsSubject')
                logging.log(DebugLevels.L6, "Trying connection: %s", wsAddr)
                ws = websocket.WebSocketApp(wsAddr,
                                            on_message=WsFeedbackReceiver.on_message,
                                            on_close=WsFeedbackReceiver.on_close,
                                            on_error=WsFeedbackReceiver.on_error,
                                            cookie="login="+WsFeedbackReceiver.sessionCookie)
                logging.log(logging.INFO, "Connected to: %s", wsAddr)
                print("Connected to: {}".format(wsAddr))
                ws.run_forever(sslopt={"ca_certs": certFile})
            except Exception as err:
                logging.log(logging.INFO, "WsFeedbackReceiver Exception {}: {}".format(type(err).__name__, str(err)))
                print('sleep {}'.format(retryInterval))
                time.sleep(retryInterval)

    @staticmethod
    def stop():
        WsFeedbackReceiver.shouldExit = True

    @staticmethod
    def on_message(client, message):
        response = {'status': 400, 'error': 'unhandled request'}
        try:
            request = json.loads(message)
            response = request.copy()
            if 'data' in response: del response['data']
            cmd = request.get('cmd')
            logging.log(logging.INFO, "{}".format(cmd))
            # Now handle requests
            if cmd == 'resultValue':
                runId = request.get('runId')
                trId = request.get('trId')
                resValue = request.get('value')
                # print("Received run: {} tr: {} value: {}".format(runId, trId, resValue))
                feedbackMsg = {'runId': runId,
                               'trId': trId,
                               'value': resValue,
                               'timestamp': time.time()
                              }
                WsFeedbackReceiver.msgQueue.put(feedbackMsg)
                response.update({'status': 200})
                return send_response(client, response)
            elif cmd == 'ping':
                response.update({'status': 200})
                return send_response(client, response)
            elif cmd == 'error':
                errorCode = request.get('status', 400)
                errorMsg = request.get('error', 'missing error msg')
                if errorCode == 401:
                    WsFeedbackReceiver.needLogin = True
                    WsFeedbackReceiver.sessionCookie = None
                errStr = 'Error {}: {}'.format(errorCode, errorMsg)
                logging.log(logging.ERROR, errStr)
                return
            else:
                errStr = 'OnMessage: Unrecognized command {}'.format(cmd)
                return send_error_response(client, response, errStr)
        except Exception as err:
            errStr = "OnMessage Exception: {}: {}".format(cmd, err)
            send_error_response(client, response, errStr)
            if cmd == 'error':
                sys.exit()
            return
        errStr = 'unhandled request'
        send_error_response(client, response, errStr)
        return

    @staticmethod
    def on_close(client):
        logging.info('connection closed')

    @staticmethod
    def on_error(client, error):
        if type(error) is KeyboardInterrupt:
            WsFeedbackReceiver.shouldExit = True
        else:
            logging.log(logging.WARNING, "on_error: WsFeedbackReceiver: {} {}".
                        format(type(error), str(error)))


def send_response(client, response):
    WsFeedbackReceiver.clientLock.acquire()
    try:
        client.send(json.dumps(response))
    finally:
        WsFeedbackReceiver.clientLock.release()


def send_error_response(client, response, errStr):
    logging.log(logging.WARNING, errStr)
    response.update({'status': 400, 'error': errStr})
    send_response(client, response)




# do arg parse for server to connect to
parser = argparse.ArgumentParser()
parser.add_argument('-s', action="store", dest="server", default="localhost:8888",
                    help="Server Address with Port [server:port]")
parser.add_argument('-i', action="store", dest="interval", type=int, default=5,
                    help="Retry connection interval (seconds)")
parser.add_argument('-u', '--username', action="store", dest="username", default=None,
                    help="rtcloud website username")
parser.add_argument('-p', '--password', action="store", dest="password", default=None,
                    help="rtcloud website password")
parser.add_argument('--test', default=False, action='store_true',
                    help='Use unsecure non-encrypted connection')
args = parser.parse_args()

if not re.match(r'.*:\d+', args.server):
    print("Error: Expecting server address in the form <servername:port>")
    parser.print_help()
    sys.exit()

addr, port = args.server.split(':')
# Check if the ssl certificate is valid for this server address
if checkSSLCertAltName(certFile, addr) is False:
    # Addr not listed in sslCert, recreate ssl Cert
    makeSSLCertFile(addr)

print("Server: {}, interval {}".format(args.server, args.interval))

WsFeedbackReceiver.startReceiverThread(args.server,
                                       retryInterval=args.interval,
                                       username=args.username,
                                       password=args.password,
                                       testMode=args.test)


#########################################################################################################
#########################################################################################################
#########################################################################################################
#########################################################################################################
######################################display feedbak images#############################################
#########################################################################################################
#########################################################################################################
#########################################################################################################
#########################################################################################################



alpha = string.ascii_uppercase

# startup parameters
IDnum = 'test' #sys.argv[1]
run = 1 #int(sys.argv[2])
scanmode = 'Test'  # 'Scan' or 'Test' or None
screenmode = False  # fullscr True or False
gui = True if screenmode == False else False
monitor_name = "testMonitor"
scnWidth, scnHeight = monitors.Monitor(monitor_name).getSizePix()
frameTolerance = 0.001  # how close to onset before 'same' frame

tune=4 # tune controls how much to morph, tune can range from (1,6.15] when paremeterrange is np.arange(1,20)
parameterRange=np.arange(1,20) #define the range for possible parameters for preloading images.

step=3 #in simulation, how quickly the morph changes ramp up

TrialNumber=3 #how many trials are required

## - design the trial list: the sequence of the different types of components: 
## - e.g: ITI + waiting for fMRI signal + feedback (receive model output from feedbackReceiver.py)
## - to look at the design, see trial_list
trial_list = pd.DataFrame(columns=['Trial','time','TR','state','newWobble'])
curTime=0
curTR=0
state=''
trial_list.append({'Trial':None,
                    'time':None,
                    'TR':None,
                    'state':None,
                    'newWobble':None},
                    ignore_index=True)
for currTrial in range(1,1+TrialNumber):
    for i in range(2): #8TR=12s
        trial_list=trial_list.append({'Trial':currTrial,
                                    'time':curTime,
                                    'TR':curTR,
                                    'state':'ITI',
                                    'newWobble':0},
                                    ignore_index=True)
        curTime=curTime+1.5
        curTR=curTR+1
    for i in range(2): #4TR=6s
        trial_list=trial_list.append({'Trial':currTrial,
                                    'time':curTime,
                                    'TR':curTR,
                                    'state':'waiting',
                                    'newWobble':0},
                                    ignore_index=True)
        curTime=curTime+1.5
        curTR=curTR+1
    for i in range(2): #8TR=12s
        newWobble=1
        for j in range(4):
            trial_list=trial_list.append({'Trial':currTrial,
                                        'time':curTime,
                                        'TR':curTR,
                                        'state':'feedback',
                                        'newWobble':newWobble},
                                        ignore_index=True)
            newWobble=0
            curTime=curTime+1.5
            curTR=curTR+1
for i in range(2): #8TR=12s
    trial_list=trial_list.append({'Trial':currTrial,
                                'time':curTime,
                                'TR':curTR,
                                'state':'ITI',
                                'newWobble':0},
                                ignore_index=True)
    curTime=curTime+1.5
    curTR=curTR+1
# Everytime there is a 1 in newWhoble, start a new timer lasting for 
# that long, and change the image source every eachTime interval.

# newWobble stands for updating the parameter of morphing intensity.
# when coming acorss a newWobble flag, retrieve the lastest parameter 
# and update the morphing intensity.

parameters = np.arange(1,step*(sum((trial_list['newWobble']==1)*1)),step) #[1,2,3,4,5,6,7,8]


print('total trial number=',TrialNumber)
# print('neighboring morph difference=',tune)
print('preloaded parameter range=',parameterRange)
print('used parameters=',parameters)


mywin = visual.Window(
    size=[1280, 800], fullscr=screenmode, screen=0,
    winType='pyglet', allowGUI=False, allowStencil=False,
    monitor='testMonitor', color=[0,0,0], colorSpace='rgb', #color=[0,0,0]
    blendMode='avg', useFBO=True,
    units='height')


# preload image list for parameter from 1 to 10.
def preloadimages(parameterRange=np.arange(1,20),tune=1):
    tune=tune-1
    start = time.time()
    imageLists={}
    numberOfUpdates=16 # corresponds to 66 updates    
    last_image=''
    for currParameter in tqdm(parameterRange): #49
        images=[]
        print('maximum morph=',round((tune*currParameter*numberOfUpdates+2)/numberOfUpdates+1))
        for axis in ['bedTable', 'benchBed']:
            for currImg in range(1,int(round(tune*currParameter*numberOfUpdates+2)),int((currParameter*numberOfUpdates+2)/numberOfUpdates)):
                currMorph=100-round(currImg/numberOfUpdates+1) if axis=='benchBed' else round(currImg/numberOfUpdates+1)
                if currMorph<1 or currMorph>99:
                    raise Exception('morphing outside limit')
                curr_image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5)
                if curr_image!=last_image:
                    currImage=visual.ImageStim(win=mywin,
                                                name='image',
                                                image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5), mask=None,
                                                ori=0, pos=(0, 0), size=(0.5, 0.5),
                                                color=[1,1,1], colorSpace='rgb', opacity=1,
                                                flipHoriz=False, flipVert=False,
                                                texRes=128, interpolate=True, depth=-4.0)
                images.append(currImage)
                last_image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5)
            for currImg in reversed(range(1,int(round(tune*currParameter*numberOfUpdates+1)),int((currParameter*numberOfUpdates+2)/numberOfUpdates))):
                currMorph=100-round(currImg/numberOfUpdates+1) if axis=='benchBed' else round(currImg/numberOfUpdates+1)
                curr_image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5)
                if curr_image!=last_image:
                    currImage=visual.ImageStim(win=mywin,
                                                name='image',
                                                image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5), mask=None,
                                                ori=0, pos=(0, 0), size=(0.5, 0.5),
                                                color=[1,1,1], colorSpace='rgb', opacity=1,
                                                flipHoriz=False, flipVert=False,
                                                texRes=128, interpolate=True, depth=-4.0)
                images.append(currImage)
                last_image='carchair_exp_feedback/{}_{}_{}.png'.format(axis,currMorph,5)
        imageLists.update( {currParameter : images} )
    end = time.time()
    print("preload image duration=", end - start)
    return imageLists
imageLists=preloadimages(parameterRange=parameterRange,tune=tune)


# Open data file for eye tracking
datadir = "./data/feedback/"

maxTR=int(trial_list['TR'].iloc[-1])+6
# Settings for MRI sequence
MR_settings = {'TR': 1.500, 'volumes': maxTR, 'sync': 5, 'skip': 0, 'sound': True} #{'TR': 1.500, 'volumes': maxTR, 'sync': 5, 'skip': 0, 'sound': True}

# check if there is a data directory and if there isn't, make one.
if not os.path.exists('./data'):
    os.mkdir('./data')
if not os.path.exists('./data/feedback/'):
    os.mkdir('./data/feedback/')

# check if data for this subject and run already exist, and raise an error if they do (prevent overwriting)
newfile = datadir+"{}_{}.csv".format(str(IDnum), str(run))

# create empty dataframe to accumulate data
data = pd.DataFrame(columns=['Sub', 'Run', 'TR', 'time'])

# Create the fixation dot, and initialize as white fill.
fix = visual.Circle(mywin, units='deg', radius=0.05, pos=(0, 0), fillColor='white',
                    lineColor='black', lineWidth=0.5, opacity=0.5, edges=128)




# start global clock and fMRI pulses (start simulated or wait for real)
print('Starting sub {} in run #{}'.format(IDnum, run))

vol = launchScan(mywin, MR_settings, simResponses=None, mode=scanmode,
                 esc_key='escape', instr='select Scan or Test, press enter',
                 wait_msg='waiting for scanner...', wait_timeout=300, log=True)

image = visual.ImageStim(
    win=mywin,
    name='image',
    image='./carchair_exp_feedback/bedChair_1_5.png', mask=None,
    ori=0, pos=(0, 0), size=(0.5, 0.5),
    color=[1,1,1], colorSpace='rgb', opacity=1,
    flipHoriz=False, flipVert=False,
    texRes=128, interpolate=True, depth=-4.0)

backgroundImage = visual.ImageStim(
    win=mywin,
    name='image',
    image='./carchair_exp_feedback/greyBackground.png', mask=None,
    ori=0, pos=(0, 0), size=(0.5, 0.5),
    color=[1,1,1], colorSpace='rgb', opacity=1,
    flipHoriz=False, flipVert=False,
    texRes=128, interpolate=True, depth=-4.0)
# trialClock is reset in each trial to change image every TR (1.5s), time for each image is 1.5/numOfImages
trialClock = core.Clock()

# trialClock.add(10)  # initialize as a big enough number to avoid text being shown at the first time.

# Trial=list(trial_list['Trial'])
# time=list(trial_list['time'])
TR=list(trial_list['TR'])
states=list(trial_list['state'])
newWobble=list(trial_list['newWobble'])

# parameters=np.round(np.random.uniform(0,10,sum((trial_list['newWobble']==1)*1)))
# parameters = np.arange(1,1+sum((trial_list['newWobble']==1)*1)) #[1,2,3,4,5,6,7,8]


ParameterUpdateDuration=np.diff(np.where(trial_list['newWobble']==1))[0][0]*1.5
# curr_parameter=0
remainImageNumber=[]

# frameTime=[]
# While the running clock is less than the total time, monitor for 5s, which is what the scanner sends for each TR
while len(TR)>1: #globalClock.getTime() <= (MR_settings['volumes'] * MR_settings['TR']) + 3:
    trialTime = trialClock.getTime()
    #frameTime.append(trialTime)
    keys = event.getKeys(["5"])  # check for triggers
    if len(keys):
        TR.pop(0)
        states.pop(0)
        newWobble.pop(0)
        #Trial.pop(0)
        #time.pop(0)
        #print('\n\n 5')
        print(states[0])
        if states[0] == 'feedback' and newWobble[0]==1:
            # fetch parameter from preprocessing process on Milgram
            # parameter=parameters[curr_parameter]
            # curr_parameter=curr_parameter+1


            #every TR(2s) you can expect to receive one value from WsFeedbackReceiver.startReceiverThread
            feedbackMsg = WsFeedbackReceiver.msgQueue.get(block=True, timeout=None)
            parameter=feedbackMsg.get('value')


            # start new clock for current updating duration (the duration in which only a single parameter is used, which can be 1 TR or a few TRs, the begining of the updateDuration is indicated by the table['newWobble'])
            trialClock=core.Clock()
            trialTime=trialClock.getTime()
            # update the image list to be shown based on the fetched parameter
            imagePaths=imageLists[parameter] #list(imageLists[parameter])
            # calculated how long each image should last.
            eachTime=ParameterUpdateDuration/len(imagePaths)
            # update the image
            # image.image=imagePaths[0]
            image.setAutoDraw(False)
            imagePaths[0].setAutoDraw(True)
            # currImage*eachTime is used in the calculation of the start time of next image in the list.
            
            # save when the image is presented and which image is presented.
            data = data.append({'Sub': IDnum, 
                                'Run': run, 
                                'TR': TR[0],
                                'time': trialTime, 
                                'imageTime':imagePaths[0].image,
                                'eachTime':eachTime},
                               ignore_index=True)
            oldMorphParameter=re.findall(r"_\w+_",imagePaths[0].image)[1]
            print('curr morph=',oldMorphParameter)
            remainImageNumber.append(0)
            currImage=1
            # # discard the first image since it has been used.
            # imagePaths.pop(0)
    if (states[0] == 'feedback') and (trialTime>currImage*eachTime):
            # image.image=imagePaths[0]
            try: # sometimes the trialTime accidentally surpasses the maximum time, in this case just do nothing, pass
                imagePaths[currImage-1].setAutoDraw(False)
                imagePaths[currImage].setAutoDraw(True)
                # print('currImage=',imagePaths[currImage],end='\n\n')
                remainImageNumber.append(currImage)

                # write the data!
                data = data.append({'Sub': IDnum, 
                                    'Run': run, 
                                    'TR': TR[0], 
                                    'time': trialTime, 
                                    'imageTime':imagePaths[currImage].image,
                                    'eachTime':eachTime},
                                    ignore_index=True)
                currMorphParameter=re.findall(r"_\w+_",imagePaths[currImage].image)[1]
                if currMorphParameter!=oldMorphParameter:
                    print('curr morph=',currMorphParameter)
                oldMorphParameter=currMorphParameter
                currImage=currImage+1        
                # imagePaths.pop(0)
            except:
                pass
    elif states[0] == 'ITI':
        backgroundImage.setAutoDraw(True)
        fix.draw()
        
# #         try:
#         print('hiding this image=',imagePaths[currImage-1],end='\n\n')
#         imagePaths[currImage-1].setAutoDraw(False)
# #         except:
# #             pass
# #         image.setAutoDraw(False)
    elif states[0] == 'waiting':
#         image.image='./carchair_exp_feedback/bedChair_1_5.png'
        backgroundImage.setAutoDraw(False)
        image.setAutoDraw(True)
        # sys.stdout.flush()
    # refresh the screen
    mywin.flip()
# print('frameTime=',frameTime)

# write data out!
data.to_csv(newfile)
mywin.close()
core.quit()


# How to update the morphing and parameter updating process?
# The morphing process is  seconds.
# parameter updating process is 2 seconds.












