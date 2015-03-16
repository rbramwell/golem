
from golem.network.transport.Tcp import Network
from TaskManager import TaskManager
from TaskComputer import TaskComputer
from TaskSession import TaskSession
from TaskBase import TaskHeader
import random
import time
import sys
import os
import logging

logger = logging.getLogger(__name__)

class TaskServer:
    #############################
    def __init__( self, address, configDesc, client ):
        self.client             = client

        self.configDesc         = configDesc

        self.address            = address
        self.curPort            = configDesc.startPort
        self.taskHeaders        = {}
        self.supportedTasks     = []
        self.removedTasks       = {}
        self.activeTasks        = {}
        self.taskManager        = TaskManager( configDesc.clientUid, rootPath = self.__getTaskManagerRoot( configDesc ), useDistributedResources = self.configDesc.useDistributedResourceManagement )
        self.taskComputer       = TaskComputer( configDesc.clientUid, self )
        self.taskSessions       = {}
        self.taskSessionsIncoming = []
        self.removedTaskTimeout = 240.0
        self.maxTrust           = 1.0
        self.minTrust           = 0.0
        self.waitingForVerification = {}

        self.lastMessages       = []

        self.resultsToSend      = {}

        self.__startAccepting()

    #############################
    def syncNetwork( self ):
        self.taskComputer.run()
        self.__removeOldTasks()
        self.__sendWaitingResults()

    #############################
    # This method chooses random task from the network to compute on our machine
    def requestTask( self ):

        if  len ( self.supportedTasks ) > 0:
            tn = random.randrange( 0, len( self.supportedTasks ) )
            taskId = self.supportedTasks[ tn ]
            theader = self.taskHeaders[ taskId ]

            self.__connectAndSendTaskRequest( self.configDesc.clientUid,
                                              theader.clientId,
                                              theader.taskOwnerAddress,
                                              theader.taskOwnerPort,
                                              theader.taskId,
                                              self.configDesc.estimatedPerformance,
                                              self.configDesc.maxResourceSize,
                                              self.configDesc.maxMemorySize,
                                              self.configDesc.numCores )

            if taskId in self.activeTasks:
                self.activeTasks[taskId]['requests'] += 1
            else:
                self.activeTasks[taskId] = {'header': theader, 'requests': 1 }


            return theader.taskId
        else:
            return 0

    #############################
    def getNodeId( self ):
        return self.configDesc.clientUid

    #############################
    def requestResource( self, subtaskId, resourceHeader, address, port ):
        self.__connectAndSendResourceRequest( address, port, subtaskId, resourceHeader )
        return subtaskId

    #############################
    def pullResources( self, taskId, listFiles ):
        self.client.pullResources( taskId, listFiles )

    #############################
    def sendResults( self, subtaskId, result, ownerAddress, ownerPort ):
        if 'data' not in result or 'resultType' not in result:
            logger.error( "Wrong result format" )
            assert False

        if subtaskId not in self.resultsToSend:
            self.resultsToSend[ subtaskId ] = WaitingTaskResult( subtaskId, result['data'], result['resultType'], 0.0, 0.0, ownerAddress, ownerPort )
        else:
            assert False

        return True

    #############################
    def newConnection(self, session):

        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager

        self.taskSessionsIncoming.append( session )

    #############################
    def getTasksHeaders( self ):
        ths =  self.taskHeaders.values() + self.taskManager.getTasksHeaders()

        ret = []

        for th in ths:
            ret.append({    "id"            : th.taskId, 
                            "address"       : th.taskOwnerAddress,
                            "port"          : th.taskOwnerPort,
                            "ttl"           : th.ttl,
                            "subtaskTimeout": th.subtaskTimeout,
                            "clientId"      : th.clientId,
                            "environment"   : th.environment,
                            "minVersion"    : th.minVersion })

        return ret

    #############################
    def addTaskHeader( self, thDictRepr ):
        try:
            id = thDictRepr[ "id" ]
            if id not in self.taskHeaders.keys(): # dont have it
                if id not in self.taskManager.tasks.keys(): # It is not my task id
                    if id not in self.removedTasks.keys(): # not removed recently
                        logger.info( "Adding task {}".format( id ) )
                        self.taskHeaders[ id ] = TaskHeader( thDictRepr[ "clientId" ], id, thDictRepr[ "address" ], thDictRepr[ "port" ], thDictRepr["environment"], thDictRepr[ "ttl" ], thDictRepr["subtaskTimeout"] )
                        if self.client.supportedTask( thDictRepr ):
                            self.supportedTasks.append( id )
            return True
        except Exception, err:
            logger.error( "Wrong task header received {}".format( str( err ) ) )
            return False

    #############################
    def removeTaskHeader( self, taskId ):
        if taskId in self.taskHeaders:
            del self.taskHeaders[ taskId ]
        if taskId in self.supportedTasks:
           self.supportedTasks.remove( taskId )
        self.removedTasks[ taskId ] = time.time()
        if taskId in self.activeTasks and self.activeTasks[taskId]['requests'] <= 0:
            del self.activeTasks[ taskId ]


    #############################
    def removeTaskSession( self, taskSession ):
        for tsk in self.taskSessions.keys():
            if self.taskSessions[ tsk ] == taskSession:
                del self.taskSessions[ tsk ]

    #############################
    def setLastMessage( self, type, t, msg, address, port ):
        if len( self.lastMessages ) >= 5:
            self.lastMessages = self.lastMessages[ -4: ]

        self.lastMessages.append( [ type, t, address, port, msg ] )

    #############################
    def getLastMessages( self ):
        return self.lastMessages

    #############################
    def getWaitingTaskResult( self, subtaskId ):
        if subtaskId in self.resultsToSend:
            return self.resultsToSend[ subtaskId ]
        else:
            return None

    #############################
    def getClientId( self ):
        return self.configDesc.clientUid

    #############################
    def getResourceAddr( self ) :
        return self.client.hostAddress

    #############################
    def getResourcePort( self ) :
        return self.client.resourcePort

    #############################
    def addResourcePeer( self, clientId, addr, port ):
        self.client.addResourcePeer( clientId, addr, port )

    #############################
    def taskResultSent( self, subtaskId ):
        if subtaskId in self.resultsToSend:
            del self.resultsToSend[ subtaskId ]
        else:
            assert False

    #############################
    def changeConfig( self, configDesc ):
        self.configDesc = configDesc
        self.taskManager.changeConfig( self.__getTaskManagerRoot( configDesc ), configDesc.useDistributedResourceManagement )
        self.taskComputer.changeConfig( )

    ############################
    def changeTimeouts( self, taskId, fullTaskTimeout, subtaskTimeout, minSubtaskTime ):
        self.taskManager.changeTimeouts( taskId, fullTaskTimeout, subtaskTimeout, minSubtaskTime )

    ############################
    def getTaskComputerRoot( self ):
        return os.path.join( self.configDesc.rootPath, "ComputerRes")

    ############################
    def subtaskRejected( self, subtaskId ):
        logger.debug( "Subtask {} result rejected".format( subtaskId ) )
        if subtaskId in self.waitingForVerification:
            self.decreaseRequesterTrust( self.waitingForVerification[ subtaskId ] )
            self.removeTaskHeader( self.waitingForVerification[ subtaskId ] )
            del self.waitingForVerification[ subtaskId ]

    ############################
    def subtaskAccepted( self, subtaskId, reward ):
        logger.debug( "Subtask {} result accepted".format( subtaskId ) )
        try:
            logger.info( "Getting {} for subtask {}".format( reward, subtaskId ) )
            self.client.getReward( int( reward ) )

        except ValueError:
            logger.error("Wrong reward amount {} for subtask {}".format( reward, subtaskId ) )
        if subtaskId in self.waitingForVerification:
            self.increaseRequesterTrust( self.waitingForVerification[ subtaskId ] )

            del self.waitingForVerification[ subtaskId ]

    ###########################
    def acceptTask(self, subtaskId, nodeId, address, port ):
        self.payForTask( subtaskId, address, port )
        self.increaseComputingTrust( nodeId, subtaskId )

    ###########################
    def increaseComputingTrust(self, nodeId, subtaskId ):
        trustMod = min( max( self.taskManager.getTrustMod( subtaskId ), self.minTrust), self.maxTrust )
        self.client.increaseComputingTrust( nodeId, trustMod )

    ###########################
    def decreaseComputingTrust(self, nodeId, subtaskId ):
        trustMod = min( max( self.taskManager.getTrustMod( subtaskId ), self.minTrust), self.maxTrust )
        self.client.decreaseComputingTrust( nodeId, trustMod )

    ###########################
    def getReceiverForTaskVerificationResult( self, taskId ):
        if taskId not in self.activeTasks:
            return None
        return self.activeTasks[taskId]['header'].clientId

    ###########################
    def receiveTaskVerification( self, taskId ):
        if taskId not in self.activeTasks:
            logger.warning("Wasn't waiting for verification result for {}").format( taskId )
            return
        self.activeTasks[ taskId ]['requests'] -= 1
        if self.activeTasks[ taskId ]['requests'] <= 0 and taskId not in self.taskHeaders:
            del self.activeTasks[ taskId ]

    ###########################
    def increaseRequesterTrust(self, taskId ):
        nodeId = self.getReceiverForTaskVerificationResult( taskId )
        self.receiveTaskVerification( taskId )
        self.client.increaseRequesterTrust( nodeId, self.maxTrust )

    ###########################
    def decreaseRequesterTrust(self, taskId ):
        nodeId = self.getReceiverForTaskVerificationResult( taskId )
        self.receiveTaskVerification( taskId )
        self.client.decreaseRequesterTrust( nodeId, self.maxTrust )

    ###########################
    def payForTask( self, subtaskId, address, port ):
        priceMod = self.taskManager.getPriceMod( subtaskId )
        price = self.client.payForTask( priceMod )
        logger.info( "Paying {} for subtask {}".format( price, subtaskId ) )
        self.__connectAndPayForTask( address, port, subtaskId, price )
        return price

    ###########################
    def rejectResult( self, subtaskId, nodeId, address, port ):
        self.decreaseComputingTrust( nodeId, subtaskId )
        self.__connectAndSendResultRejected( subtaskId, address, port )

    ###########################
    def unpackDelta( self, destDir, delta, taskId ):
        self.client.resourceServer.unpackDelta( destDir, delta, taskId )

    #############################
    # PRIVATE SECTION

    #############################
    def __startAccepting(self):
        logger.info( "Enabling tasks accepting state" )
        Network.listen( self.configDesc.startPort, self.configDesc.endPort, TaskServerFactory( self ), None, self.__listeningEstablished, self.__listeningFailure  )

    #############################
    def __listeningEstablished( self, iListeningPort ):
        port = iListeningPort.getHost().port
        self.curPort = port
        logger.info( "Port {} opened - listening".format( port ) )
        self.taskManager.listenAddress = self.address
        self.taskManager.listenPort = self.curPort

    #############################
    def __listeningFailure(self, p):
        self.curPort = 0
        logger.error( "Task server not listening" )
        #FIXME: some graceful terminations should take place here
        # sys.exit(0)

    #############################   
    def __connectAndSendTaskRequest( self, clientId, taskClientId, address, port, taskId, estimatedPerformance, maxResourceSize, maxMemorySize, numCores ):
        Network.connect( address, port, TaskSession, self.__connectionForTaskRequestEstablished, self.__connectionForTaskRequestFailure, clientId, taskId, estimatedPerformance, maxResourceSize, maxMemorySize, numCores )

    #############################   
    def __connectAndSendResourceRequest( self, address ,port, subtaskId, resourceHeader ):
        Network.connect( address, port, TaskSession, self.__connectionForResourceRequestEstablished, self.__connectionForResourceRequestFailure, subtaskId, resourceHeader )

    #############################
    def __connectAndSendResultRejected( self, subtaskId, address, port ):
        Network.connect( address, port, TaskSession, self.__connectionForSendResultRejectedEstablished, self.__connectionForResultRejectedFailure, subtaskId )

    #############################
    def __connectAndPayForTask( self, address, port, subtaskId, price ):
        Network.connect( address, port, TaskSession, self.__connectionForPayForTaskEstablished, self.__connectionForPayForTaskFailure, subtaskId, price )

    #############################
    def __connectionForTaskRequestEstablished( self, session, clientId, taskId, estimatedPerformance, maxResourceSize, maxMemorySize, numCores ):

        session.taskId = taskId
        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager
        self.taskSessions[ taskId ] = session
        session.requestTask( clientId, taskId, estimatedPerformance, maxResourceSize, maxMemorySize, numCores )

    #############################
    def __connectionForTaskRequestFailure( self, clientId, taskId, estimatedPerformance, *args ):
        logger.warning( "Cannot connect to task {} owner".format( taskId ) )
        logger.warning( "Removing task {} from task list".format( taskId ) )
        
        self.taskComputer.taskRequestRejected( taskId, "Connection failed" )
        if taskId in self.activeTaskHeaders:
            self.activeTaskHeaders[ taskId ]['requests'] -= 1
        self.removeTaskHeader( taskId )

    #############################   
    def __connectAndSendTaskResults( self, address, port, waitingTaskResult ):
        Network.connect( address, port, TaskSession, self.__connectionForTaskResultEstablished, self.__connectionForTaskResultFailure, waitingTaskResult )

    #############################
    def __connectionForTaskResultEstablished( self, session, waitingTaskResult ):

        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager

        self.taskSessions[ waitingTaskResult.subtaskId ] = session

        session.sendReportComputedTask( waitingTaskResult, self.address, self.curPort )

    #############################
    def __connectionForTaskResultFailure( self, waitingTaskResult ):
        logger.warning( "Cannot connect to task {} owner".format( waitingTaskResult.subtaskId ) )
        logger.warning( "Removing task {} from task list".format( waitingTaskResult.subtaskId ) )
        
        waitingTaskResult.lastSendingTrial  = time.time()
        waitingTaskResult.delayTime         = self.configDesc.maxResultsSendingDelay
        waitingTaskResult.alreadySending    = False

    #############################
    def __connectionForResourceRequestEstablished( self, session, subtaskId, resourceHeader ):

        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager
        self.taskSessions[ subtaskId ] = session
        session.taskId = subtaskId
        session.requestResource( subtaskId, resourceHeader )

    #############################
    def __connectionForResourceRequestFailure( self, session, subtaskId, resourceHeader ):
        logger.warning( "Cannot connect to task {} owner".format( subtaskId ) )
        logger.warning( "Removing task {} from task list".format( subtaskId ) )
        
        self.taskComputer.resourceRequestRejected( subtaskId, "Connection failed" )
        
        self.removeTaskHeader( subtaskId )

    #############################
    def __connectionForResultRejectedFailure( self, subtaskId ):
        logger.warning( "Cannot connect to deliver information about rejected result for task {}".format( subtaskId ) )

    #############################
    def __connectionForPayForTaskFailure( self,subtaskId, price ):
        logger.warning( "Cannot connect to pay for task {} ".format( subtaskId ) )
        #TODO
        # Taka informacja powinna byc przechowywana i proba oplaty powinna byc wysylana po jakims czasie

    def __connectionForSendResultRejectedEstablished( self, session, subtaskId ):
        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager
        session.sendResultRejected( subtaskId )

    def __connectionForPayForTaskEstablished( self, session, subtaskId, price ):
        session.taskServer = self
        session.taskComputer = self.taskComputer
        session.taskManager = self.taskManager
        session.sendRewardForTask( subtaskId, price )

    #############################
    def __removeOldTasks( self ):
        for t in self.taskHeaders.values():
            currTime = time.time()
            t.ttl = t.ttl - ( currTime - t.lastChecking )
            t.lastChecking = currTime
            if t.ttl <= 0:
                logger.warning( "Task {} dies".format( t.taskId ) )
                self.removeTaskHeader( t.taskId )

        self.taskManager.removeOldTasks()

        for id, removeTime in self.removedTasks.items():
            currTime = time.time()
            if  currTime - removeTime > self.removedTaskTimeout:
                del self.removedTasks[ id ]

    def __sendWaitingResults( self ):
        for wtr in self.resultsToSend:
            waitingTaskResult = self.resultsToSend[ wtr ]

            if not waitingTaskResult.alreadySending:
                if time.time() - waitingTaskResult.lastSendingTrial > waitingTaskResult.delayTime:
                    subtaskId = waitingTaskResult.subtaskId

                    waitingTaskResult.alreadySending = True
                    self.__connectAndSendTaskResults( waitingTaskResult.ownerAddress, waitingTaskResult.ownerPort, waitingTaskResult )

    def __getTaskManagerRoot( self, configDesc ):
        return os.path.join( configDesc.rootPath, "res" )

class WaitingTaskResult:
    #############################
    def __init__( self, subtaskId, result, resultType, lastSendingTrial, delayTime, ownerAddress, ownerPort  ):
        self.subtaskId          = subtaskId
        self.result             = result
        self.resultType         = resultType
        self.lastSendingTrial   = lastSendingTrial
        self.delayTime          = delayTime
        self.ownerAddress       = ownerAddress
        self.ownerPort          = ownerPort
        self.alreadySending     = False

from twisted.internet.protocol import Factory
from TaskConnState import TaskConnState

class TaskServerFactory(Factory):
    #############################
    def __init__( self, server ):
        self.server = server

    #############################
    def buildProtocol(self, addr):
        logger.info( "Protocol build for {}".format( addr ) )
        return TaskConnState( self.server )