# -*- coding:utf-8 -*-
'''
Created on 2015年12月1日

@author: zhaojiangang
'''
import copy

from datetime import datetime
import time

from dizhu.entity import dizhuonlinedata, dizhuhallinfo, dizhushare
from dizhu.entity.dizhuconf import DIZHU_GAMEID, RED_ENVELOPE
from dizhu.entity.dizhuversion import SessionDizhuVersion
from dizhu.entity.matchrecord import MatchRecord
from dizhu.entity.led_util import LedUtil
from dizhu.entity import dizhuled
from dizhu.entity.official_counts.events import OfficialMessageEvent
from dizhu.entity.reward_async.events import UserRewardAsyncEvent
from dizhu.entity.reward_async.reward_async import REWARD_ASYNC_TYPE_AS_ARENA_MATCH, RewardAsyncHelper
from dizhu.entity.wx_share_control import WxShareControlHelper
from hall.entity.usercoupon import user_coupon_details
from hall.entity.usercoupon.events import UserCouponReceiveEvent
from dizhu.games import matchutil, match_signin_discount
from dizhu.games.matchutil import MatchLottery, RedEnvelopeHelper
from dizhucomm.entity import commconf
from freetime.entity.msg import MsgPack
import freetime.util.log as ftlog
from hall.entity import datachangenotify, hallitem
from hall.entity.hallconf import HALL_GAMEID
from hall.entity.todotask import TodoTaskShowInfo, TodoTaskHelper
from hall.servers.util.rpc import user_remote, event_remote
import poker.entity.biz.message.message as pkmessage
from poker.entity.biz.message import message
from poker.entity.configure import gdata, configure
from poker.entity.dao import userdata, sessiondata, daobase, onlinedata, daoconst
from poker.entity.game.rooms.arena_match_ctrl.exceptions import \
    SigninFeeNotEnoughException
from poker.entity.game.rooms.arena_match_ctrl.interfaces import \
    MatchPlayerNotifier, MatchTableController, UserInfoLoader, \
    MatchRankRewardsSender, SigninFee, SigninRecordDao, UserLocker, MatchRankRewardsSelector
from poker.entity.game.rooms.arena_match_ctrl.match import MatchRankRewards
from poker.entity.game.rooms.big_match_ctrl.const import MatchFinishReason, \
    AnimationType
from poker.protocol import router
from poker.util import strutil
import poker.util.timestamp as pktimestamp
from poker.entity.events import hall51event


class UserLockerDizhu(UserLocker):
    def lockUser(self, userId, roomId, tableId, seatId, clientId):
        locList = dizhuonlinedata.getOnlineLocListByGameId(userId, DIZHU_GAMEID, clientId)
        if locList:
            for loc in locList:
                if (strutil.getBigRoomIdFromInstanceRoomId(loc[1])
                    != strutil.getBigRoomIdFromInstanceRoomId(roomId)):
                    ftlog.warn('UserLockerDizhu.lockUser',
                               'userId=', userId,
                               'roomId=', roomId,
                               'tableId=', tableId,
                               'seatId=', seatId,
                               'clientId=', clientId,
                               'loc=', loc,
                               'locList=', locList)
                    return False
        room = gdata.rooms()[roomId]
        player = room.match.findPlayer(userId)
        if not player or not player.isQuit:
            onlinedata.setBigRoomOnlineLoc(userId, roomId, tableId, seatId)
        return True
    
    def unlockUser(self, userId, roomId, tableId, clientId):
        onlinedata.removeOnlineLoc(userId, roomId, tableId)
    
class MatchPlayerNotifierDizhu(MatchPlayerNotifier):
    def __init__(self, room):
        self._room = room
        
    def notifyMatchStart(self, player):
        '''
        通知用户比赛开始了
        '''
        try:
            ftlog.info('MatchPlayerNotifierDizhu.notifyMatchStart matchId=', player.matchInst.matchId,
                       'signinParams=', player.signinParams,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId)
            mo = MsgPack()
            mo.setCmd('m_start')
            mo.setResult('gameId', self._room.gameId)
            mo.setResult('roomId', self._room.bigRoomId)
            router.sendToUser(mo, player.userId)
            
            mo = MsgPack()
            mo.setCmd('m_play_animation')
            mo.setResult('gameId', self._room.gameId)
            mo.setResult('roomId', self._room.bigRoomId)
            mo.setResult('type', AnimationType.ASSIGN_TABLE)
            router.sendToUser(mo, player.userId)
            
            sequence = int(player.matchInst.instId.split('.')[1])
            matchutil.report_bi_game_event('MATCH_START', player.userId, self._room.bigRoomId, 0, sequence, 0, 0, 0, [int(player.mixId) if player.mixId else 255], 'match_start')

            # 保存用户折扣次数
            roomDiscountConf = match_signin_discount.getRoomDiscountConf(player.mixId if player.mixId else self._room.bigRoomId)
            if roomDiscountConf and player._paidFee:
                ret, _, _ = match_signin_discount.canMatchDiscount(player.userId, player.mixId if player.mixId else self._room.bigRoomId, player.paidFee.assetKindId)
                if ret:
                    match_signin_discount.saveUserMatchDiscountCount(player.userId, player.mixId if player.mixId else self._room.bigRoomId, player._paidFee.assetKindId)
        except:
            ftlog.error()
            
    def notifyMatchUpdate(self, player):
        if player.isQuit:
            return
        try:
            self._room.sendMatchStatas(player.userId)
        except:
            ftlog.error()
            
    def notifyMatchWait(self, player):
        '''
        通知用户等待晋级
        '''
        try:
            if player.isQuit:
                return
            ftlog.info('MatchPlayerNotifierDizhu.notifyMatchWait matchId=', player.matchInst.matchId,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId,
                       'signinParams=', player.signinParams,
                       'stageIndex=', player.stage.index)
            self.notifyMatchUpdate(player)
            self.notifyMatchRank(player)
            msg = MsgPack()
            msg.setCmd('m_wait')
            msg.setResult('gameId', self._room.gameId)
            msg.setResult('roomId', self._room.bigRoomId)
            msg.setResult('mixId', player.mixId)
            msg.setResult('tableId', player.match.tableId)
            arenaContent = dizhuhallinfo.getArenaMatchProvinceContent(player.userId, int(player.mixId) if player.mixId else self._room.bigRoomId, None)
            roomName = player.matchInst.matchConf.getRoomName(player.mixId)
            if arenaContent:
                roomName = arenaContent.get('showName') or roomName
            msg.setResult('mname', roomName)
            prevStage = player.stage.prevStage or player.stage
            msg.setResult('riseCount', prevStage.stageConf.riseUserCount)
            steps = []
            for stage in player.matchInst.stages:
                isCurrent = True if stage == prevStage else False
                des = '%s人晋级' % (stage.stageConf.riseUserCount)
                stepInfo = {'des':des}
                if isCurrent:
                    stepInfo['isCurrent'] = 1
                stepInfo['name'] = stage.stageConf.name
                steps.append(stepInfo)
                
            msg.setResult('steps', steps)
            msg.setResult('rise', player.cardCount == 0) # arena比赛已经确定升级player.stage.stageConf.cardCount
            msg.setResult('matchType', 'arena_match') # arena比赛
            router.sendToUser(msg, player.userId)
        except:
            ftlog.error()
        
    def notifyMatchRank(self, player):
        '''
        通知比赛排行榜
        '''
        try:
            if player.isQuit:
                return
            assert(player.stage)
            ftlog.info('MatchPlayerNotifierDizhu.notifyMatchRank matchId=', player.matchInst.matchId,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId,
                       'signinParams=', player.signinParams,
                       'stageIndex=', player.stage.index)
            msg = MsgPack()
            msg.setCmd('m_rank')
            ranktops = []
            ranktops.append({'userId':player.userId,
                             'name':player.userName,
                             'score':player.score,
                             'rank':player.rank})
            msg.setResult('mranks', ranktops)
            msg.setResult('roomId', self._room.bigRoomId)
            msg.setResult('mixId', player.mixId)
            router.sendToUser(msg, player.userId)
        except:
            ftlog.error()
            
    def notifyMatchGiveupFailed(self, player, message):
        '''
        通知用户放弃比赛失败
        '''
        try:
            if player.isQuit:
                return
            msg = MsgPack()
            msg.setCmd('room')
            msg.setError(-1, message)
            router.sendToUser(msg, player.userId)
        except:
            ftlog.error()

    def notifyMatchWillCancelled(self, player, reason):
        '''
        通知用户比赛即将取消
        '''
        try:
            if player.isQuit:
                return
            ftlog.info('MatchPlayerNotifierDizhu.notifyMatchWillCancelled matchId=', player.matchInst.matchId,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId,
                       'signinParams=', player.signinParams,
                       'stageIndex=', player.stage.index if player.stage else None,
                       'reason=', reason)
            TodoTaskHelper.sendTodoTask(6, player.userId, TodoTaskShowInfo(reason, True))
        except:
            ftlog.error()

    def notifyMatchCancelled(self, player, reason, info):
        '''
        通知用户比赛取消
        '''
        try:
            if player.isQuit:
                return
            msg = MsgPack()
            msg.setCmd('m_over')
            msg.setResult('mixId', player.mixId)
            msg.setResult('gameId', self._room.gameId)
            msg.setResult('roomId', self._room.bigRoomId)
            msg.setResult('reason', reason)
            msg.setResult('info', info)
            router.sendToUser(msg, player.userId)
        except:
            ftlog.error()
    
    def buildWinInfo(self, player, rankRewards):
        roomName = player.matchInst.matchConf.getRoomName(player.mixId)
        return '比赛：%s\n名次：第%d名\n奖励：%s\n奖励已发放，请您查收。' % (roomName, player.rank, rankRewards.desc)

    def buildLoserInfo(self, player):
        roomName = player.matchInst.matchConf.getRoomName(player.mixId)
        return '比赛：%s\n名次：第%d名\n胜败乃兵家常事 大侠请重新来过！' % (roomName, player.rank)

    def notifyMatchOver(self, player, reason, rankRewards):
        '''
        通知用户比赛结束了
        '''
        try:
            exchangeMoney = rankRewards.conf.get('exchangeMoney', None) if rankRewards else None
            exchangeCode = None
            if exchangeMoney:
                exchangeCode = RedEnvelopeHelper.getRedEnvelopeCode(player.userId, self._room.gameId, exchangeMoney, self._room.roomId, self._room.match.matchId, player.rank)

            ftlog.info('MatchPlayerNotifierDizhu.notifyMatchOver matchId=', player.matchInst.matchId,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId,
                       'signinParams=', player.signinParams,
                       'stageIndex=', player.stage.index,
                       'rank=', player.rank,
                       'reason=', reason,
                       'rankRewards=', rankRewards,
                       'exchangeCode=', exchangeCode)

            lastBestRank = None
            record = MatchRecord.loadRecord(self._room.gameId, player.userId, self._room.match.matchId)
            if record:
                lastBestRank = record.bestRank

            # 获取房间名
            roomName = player.matchInst.matchConf.getRoomName(player.mixId)
            arenaContent = dizhuhallinfo.getArenaMatchProvinceContent(player.userId, int(player.mixId) if player.mixId else self._room.bigRoomId, None)
            if arenaContent:
                roomName = arenaContent.get('showName') or roomName

            rewardId = None
            if (reason == MatchFinishReason.USER_WIN
                or reason == MatchFinishReason.USER_LOSER):
                try:
                    if player.isQuit:
                        rankRewards = None
                    event_remote.publishMatchWinloseEvent(self._room.gameId,
                                                          player.userId,
                                                          self._room.match.matchId,
                                                          reason == MatchFinishReason.USER_WIN,
                                                          player.rank,
                                                          player.matchInst.matchConf.stages[0].totalUserCount,
                                                          rankRewards.conf if rankRewards else None)

                    tempGameResult = 1 if reason == MatchFinishReason.USER_WIN else -1
                    hall51event.sendToHall51MatchOverEvent(player.userId, self._room.gameId, self._room.bigRoomId, tempGameResult, -1, -1)

                    if rankRewards:
                        from dizhu.entity.matchhistory import MatchHistoryHandler
                        MatchHistoryHandler.onMatchOver(player.userId,
                                                        player.matchInst.matchConf.recordId,
                                                        player.rank,
                                                        reason == MatchFinishReason.USER_WIN,
                                                        rankRewards.conf if rankRewards else None,
                                                        False,
                                                        player.mixId,
                                                        exchangeCode=exchangeCode)

                        matchRewardSwitch = WxShareControlHelper.getMatchRewardSwitch()
                        if matchRewardSwitch:
                            from dizhu.game import TGDizhu
                            rewardId = RewardAsyncHelper.genRewardId()
                            rewards = []
                            for rewardInfo in rankRewards.rewards:
                                rewards.append({'itemId': rewardInfo['itemId'], 'count': rewardInfo['count']})
                            playerMixId = int(player.mixId) if player.mixId else None
                            TGDizhu.getEventBus().publishEvent(UserRewardAsyncEvent(DIZHU_GAMEID,
                                                                                    player.userId,
                                                                                    REWARD_ASYNC_TYPE_AS_ARENA_MATCH,
                                                                                    rewardId,
                                                                                    rewards,
                                                                                    matchId=playerMixId or player.matchInst.matchId,
                                                                                    mixId=playerMixId or 0,
                                                                                    rank=player.rank,
                                                                                    sequence=int(player.matchInst.instId.split('.')[1]),
                                                                                    roomName=roomName))

                    if not rankRewards:
                        matchutil.report_bi_game_event('MATCH_REWARD', player.userId, player.matchInst.matchId, 0, int(player.matchInst.instId.split('.')[1]),
                                                       0, 0, 0,
                                                       [0, 0, 0, player.rank, int(player.mixId) if player.mixId else 0, 0],
                                                       'match_reward')

                except:
                    ftlog.error()

                # 比赛记录保存
                try:
                    event = {'gameId':self._room.gameId,
                             'userId':player.userId,
                             'matchId':self._room.match.matchId,
                             'rank':player.rank,
                             'isGroup': 0,
                             'mixId':player.mixId}
                    MatchRecord.updateAndSaveRecord(event)
                except:
                    ftlog.error()

            msg = MsgPack()
            msg.setCmd('m_over')
            msg.setResult('rewardId', rewardId)
            msg.setResult('mixId', player.mixId)
            msg.setResult('gameId', self._room.gameId)
            msg.setResult('roomId', self._room.bigRoomId)
            msg.setResult('userId', player.userId)
            msg.setResult('reason', reason)
            msg.setResult('rank', player.rank)
            msg.setResult('exchangeCode', exchangeCode)

            if rankRewards:
                msg.setResult('info', self.buildWinInfo(player, rankRewards))
            else:
                msg.setResult('info', self.buildLoserInfo(player))
            msg.setResult('mucount', player.matchInst.matchConf.stages[0].totalUserCount)
            msg.setResult('date', str(datetime.now().date().today()))
            msg.setResult('time', time.strftime('%H:%M', time.localtime(time.time())))
            msg.setResult('addInfo', '')
            rewardDesc = ''
            if rankRewards:
                msg.setResult('reward', matchutil.buildRewards(rankRewards))
                rewardDesc = matchutil.buildRewardsDesc(rankRewards)
                if rewardDesc:
                    msg.setResult('rewardDesc', rewardDesc)

            msg.setResult('mname', roomName)

            clientId = sessiondata.getClientId(player.userId)

            # 微信分享
            money = 0
            if rankRewards:
                for r in rankRewards.rewards:
                    if r['itemId'] == 'user:coupon':
                        assetKind = hallitem.itemSystem.findAssetKind('user:coupon')
                        displayRate = assetKind.displayRate
                        money = round(r['count'] * 1.0 / displayRate, 2)
            kwargs = {
                'matchRank': {
                    'money': money % 100,
                    'stop': player.rank
                }
            }

            shareInfo = commconf.getNewShareInfoByCondiction(self._room.gameId, clientId)
            msg.setResult('shareInfo', {'erweima': shareInfo['erweima'] if shareInfo else {}})

            try:
                # 玩家红包记录
                dizhushare.addMatchHistoryCount(self._room.bigRoomId, player.rank)
                userShareInfo = rankRewards.conf.get('shareInfo', {}) if rankRewards else {}
                rewardType, shareInfoNew = dizhushare.getArenaShareInfoNew(player.userId,
                                                               player.matchInst.matchConf.feeRewardList,
                                                               arenaContent,
                                                               userShareInfo)
                if shareInfoNew:
                    msg.setResult('shareInfoNew', shareInfoNew)

                # 设置奖状分享的todotask diplomaShare
                matchShareType = 'arena' if rewardType == 'redEnvelope' else 'group'
                shareTodoTask = commconf.getMatchShareInfo(player.userName, roomName, player.rank, rewardDesc,
                                                           player.userId, matchShareType, clientId)
                if shareTodoTask:
                    msg.setResult('shareTodoTask', shareTodoTask)

                if rankRewards:
                    bigImg = rankRewards.conf.get('bigImg', '')
                    if bigImg:
                        msg.setResult('bidImg', bigImg)

                msg.setResult('beatDownUser', player.beatDownUserName)
                if rankRewards and rankRewards.todotask:
                    msg.setResult('todotask', rankRewards.todotask)

                # 微信公众号消息
                if player.rank == 1:
                    from hall.game import TGHall
                    msgParams = {
                        'reward': rewardDesc,
                        'roomName': roomName}
                    TGHall.getEventBus().publishEvent(
                        OfficialMessageEvent(DIZHU_GAMEID, player.userId, RED_ENVELOPE, msgParams, mixId=player.mixId))
                    if ftlog.is_debug():
                        ftlog.debug('MatchPlayerNotifierDizhu.notifyMatchOver.redEnvelopeEvent userId=', player.userId,
                                    'reward=', rewardDesc,
                                    'rank=', player.rank,
                                    'roomName=', roomName)

                # 冠军触发抽奖逻辑
                match_lottery = MatchLottery()
                ret = match_lottery.checkMatchRank(player.userId, self._room.match.matchId, player.rank)
                if ret:
                    msg.setResult('match_lottery', 1)

                # 局间奖励总数
                if player.stageRewardTotal:
                    msg.setResult('stageReward', {'count': player.stageRewardTotal})

                if ftlog.is_debug():
                    ftlog.debug('MatchPlayerNotifierDizhu.notifyMatchOver userId=', player.userId,
                                'roomId=', self._room.roomId,
                                'rank=', player.rank,
                                'player=', player,
                                'stageRewardTotal=', player.stageRewardTotal)

            except Exception, e:
                ftlog.error('notifyMatchOver.getArenaShareInfoNew',
                            'userId=', player.userId,
                            'matchId=', self._room.match.matchId,
                            'err=', e.message)


            record = MatchRecord.loadRecord(self._room.gameId, player.userId, self._room.match.matchId)
            if record:
                msg.setResult('mrecord', {'bestRank':record.bestRank,
                                          'lastBestRank':lastBestRank,
                                          'bestRankDate':record.bestRankDate,
                                          'isGroup':record.isGroup,
                                          'crownCount':record.crownCount,
                                          'playCount':record.playCount})
            else:
                from dizhu.activities.toolbox import Tool
                msg.setResult('mrecord', {'bestRank':player.rank,
                                          'lastBestRank': lastBestRank,
                                          'bestRankDate':Tool.datetimeToTimestamp(datetime.now()),
                                          'isGroup': 0,
                                          'crownCount':1 if player.rank == 1 else 0,
                                          'playCount':1})

            if not player.isQuit:
                router.sendToUser(msg, player.userId)

            # 混房冠军LED
            mixId = player.mixId
            if mixId:
                mixConf = MatchPlayerNotifierDizhu.getArenaMixConf(self._room.roomConf, mixId)
                if player.rank == 1 and mixConf.get('championLed'):
                    arenaContent = dizhuhallinfo.getArenaMatchProvinceContent(player.userId,
                                                                              int(mixId) if mixId else self._room.roomId,
                                                                              clientId)
                    if ftlog.is_debug():
                        ftlog.debug('MatchPlayerNotifierDizhu.notifyMatchOver',
                                    'userId=', player.userId,
                                    'roomId=', self._room.roomId,
                                    'mixId=', mixId,
                                    'roomName', mixConf.get('roomName'),
                                    'rewardShow=', mixConf.get('rewardShow', rewardDesc),
                                    'mixConf=', mixConf)
                    # 冠军发送Led通知所有其他玩家
                    ledtext = dizhuled._mk_match_champion_rich_text(
                        player.userName,
                        arenaContent.get('name') if arenaContent else mixConf.get('roomName'),
                        arenaContent.get('rewardShow') if arenaContent else mixConf.get('rewardShow', rewardDesc)
                    )
                    LedUtil.sendLed(ledtext, 'global')
            else:
                if player.rank == 1 and self._room.roomConf.get('championLed') and not player.isQuit:
                    arenaContent = dizhuhallinfo.getArenaMatchProvinceContent(player.userId,
                                                                              int(mixId) if mixId else self._room.roomId,
                                                                              clientId)
                    # 冠军发送Led通知所有其他玩家
                    ledtext = dizhuled._mk_match_champion_rich_text(
                        player.userName,
                        arenaContent.get('name') if arenaContent else roomName,
                        arenaContent.get('rewardShow') if arenaContent else self._room.roomConf.get('rewardShow', rewardDesc)
                    )
                    LedUtil.sendLed(ledtext, 'global')

            sequence = int(player.matchInst.instId.split('.')[1])
            matchutil.report_bi_game_event('MATCH_FINISH', player.userId, player.matchInst.matchId, 0, sequence, 0, 0, 0, [int(player.mixId) if player.mixId else 255], 'match_end')
        except:
            ftlog.error()

    def getUserChampionLimitFlag(self, player):
        '''
        是否需要积分大衰减
        '''
        return matchutil.getUserChampionLimitFlag(player.userId, player.matchInst.matchId, player.matchInst.matchConf.recordId, player.mixId)

    def notifyMatchUserRevive(self, player, reviveContent):
        # 如果是机器人立即直接复活
        if self.isRobot(player):
            self._room.match.currentInstance.doUserRevive(player, True)
            return

        afterScore = player.stage.stageConf.rankLine.getMinScoreByRank(reviveContent.get('rank'))
        afterRank = reviveContent.get('rank')
        # 判断用户是否需要高倍衰减
        if player.championLimitFlag:
            rate = player.stage.nextStage.stageConf.scoreIntoRateHigh if player.stage.nextStage.stageConf.scoreIntoRateHigh else player.nextStage.stage.stageConf.scoreIntoRate
            afterScore = afterScore * rate
        else:
            afterScore = afterScore * player.stage.nextStage.stageConf.scoreIntoRate

        msg = MsgPack()
        msg.setCmd('m_revival')
        msg.setResult('mixId', player.mixId)
        msg.setResult('gameId', self._room.gameId)
        msg.setResult('roomId', self._room.bigRoomId)
        msg.setResult('userId', player.userId)
        msg.setResult('rank', player.rank)
        msg.setResult('afterRank', afterRank)
        msg.setResult('score', player.score)
        msg.setResult('afterScore', int(afterScore))
        msg.setResult('riseUserCount', player.stage.stageConf.riseUserCount)
        msg.setResult('timeLeft', int(player.reviveExpirationTime - pktimestamp.getCurrentTimestamp()))
        fee = reviveContent['fee']
        itemId = fee['itemId']
        userAssets = hallitem.itemSystem.loadUserAssets(player.userId)
        timestamp = pktimestamp.getCurrentTimestamp()
        balance = userAssets.balance(HALL_GAMEID, itemId, timestamp)
        assetKind = hallitem.itemSystem.findAssetKind(itemId)
        msg.setResult('fee', {'feeCount': fee['count'], 'leftFeeCount':  balance, 'pic': fee.get('img') if fee.get('img') else assetKind.pic})

        if ftlog.is_debug():
            ftlog.debug('MatchPlayerNotifierDizhu.notifyMatchUserRevive',
                        'instId=', player.matchInst.instId,
                        'userId=', player.userId,
                        'signinParams=', player.signinParams,
                        'stageIndex=', player.stage.index,
                        'msg=', msg.pack(),
                        'rank=', player.rank)
        router.sendToUser(msg, player.userId)

    @classmethod
    def getArenaMixConf(cls, conf, mixId):
        for mixConf in conf.get('matchConf', {}).get('feeRewardList', []):
            if mixConf.get('mixId') == mixId:
                return mixConf
        return {}

    @classmethod
    def isRobot(cls, player):
        if not player or player.userId <= 0:
            return False

        from poker.entity.game.tables.table_player import ROBOT_USER_ID_MAX
        from sre_compile import isstring

        if player.userId > 0 and player.userId <= ROBOT_USER_ID_MAX:
            return True

        if (player.userId > ROBOT_USER_ID_MAX
            and isstring(player.clientId)
            and player.clientId.find('robot') >= 0):
            return True
        return False

class SigninRecordDaoDizhu(SigninRecordDao):
    def __init__(self, room):
        self._room = room
        
    def recordSignin(self, matchId, instId, userId, timestamp, signinParams):
        '''
        记录报名信息
        '''
        try:
            daobase.executeTableCmd(self._room.roomId, 0, 'SADD', 'signs:' + str(self._room.roomId), userId)
        except:
            ftlog.error()
    
    def removeSignin(self, matchId, instId, userId):
        '''
        删除报名信息
        '''
        try:
            daobase.executeTableCmd(self._room.roomId, 0, 'SREM', 'signs:' + str(self._room.roomId), userId)
        except:
            ftlog.error()
    
    def removeAll(self, matchId):
        '''
        删除instId相关的所有报名信息
        '''
        try:
            daobase.executeTableCmd(self._room.roomId, 0, 'DEL', 'signs:' + str(self._room.roomId))
        except:
            ftlog.error()
            
class MatchTableControllerDizhu(MatchTableController):
    def __init__(self, room):
        self._room = room
        
    def startTable(self, table):
        '''
        让桌子开始
        '''
        try:
            mo = MsgPack()
            mo.setCmd('table_manage')
            mo.setAction('m_table_start')
            mo.setParam('gameId', table.gameId)
            mo.setParam('roomId', table.roomId)
            mo.setParam('tableId', table.tableId)
            mo.setParam('matchId', table.matchInst.matchId)
            mo.setParam('ccrc', table.ccrc)
            mo.setParam('baseScore', table.matchInst.matchConf.baseScore)
            userInfos = []
            ranks = []
            for seat in table.seats:
                player = seat.player
                if player:
                    ranks.append(player.tableDisplayRank)
                    hasEnterRewards = False
                    try:
                        if ftlog.is_debug():
                            ftlog.debug('startTable playerId=', player.userId,
                                        'stageIndex=', player.stage.index,
                                        'hasRewardIndex=', self._room.roomConf.get('hasRewardIndex'))
                        if player.stage.stageConf.cardCount == 1 and not player.isQuit and player.stage.index == self._room.roomConf.get('hasRewardIndex'):
                            hasEnterRewards = True
                    except Exception, e:
                        ftlog.error('hasEnterRewards ',
                                    'instId=', table.matchInst.instId,
                                    'roomId=', table.roomId,
                                    'err=', e.message)
                    userInfo = {
                        'userId':player.userId,
                        'seatId':seat.seatId,
                        'score':player.score,
                        'mixId':player.mixId,
                        'isQuit':player.isQuit,
                        'cardCount':player.cardCount,
                        'rank':player.tableDisplayRank,
                        'isLastStage': table.matchInst._isLastStage(player.stage),
                        'chiprank':player.tableDisplayRank,
                        'userName':player.userName,
                        'clientId':player.clientId,
                        'roomName':table.matchInst.matchConf.getRoomName(player.mixId),
                        'winloseForTuoguan':player.winloseForTuoguan,
                        'ranks':ranks,
                        'hasEnterRewards': hasEnterRewards,
                        'stage':{
                            'name':player.stage.stageConf.name,
                            'index':player.stage.stageConf.index,
                            'cardCount':player.stage.stageConf.cardCount,
                            'playerCount':player.stage.stageConf.totalUserCount,
                            'riseCount':player.stage.stageConf.riseUserCount,
                            'animationType':player.stage.stageConf.animationType
                        },
                        'stageRewardTotal': seat.player.stageRewardTotal
                    }
                else:
                    userInfo = None
                userInfos.append(userInfo)
            mo.setParam('userInfos', userInfos)
            if ftlog.is_debug():
                ftlog.debug('MatchTableControllerDizhu.startTable matchId=', table.matchInst.matchId,
                            'instId=', table.matchInst.instId,
                            'roomId=', table.roomId,
                            'tableId=', table.tableId,
                            'mo=', mo)
            router.sendTableServer(mo, table.roomId)
        except:
            ftlog.error()
            
    def clearTable(self, table):
        '''
        清理桌子
        '''
        try:
            msg = MsgPack()
            msg.setCmd('table_manage')
            msg.setParam('action', 'm_table_clear')
            msg.setParam('gameId', table.gameId)
            msg.setParam('matchId', table.matchInst.matchId)
            msg.setParam('roomId', table.roomId)
            msg.setParam('tableId', table.tableId)
            msg.setParam('ccrc', table.ccrc)
            if ftlog.is_debug():
                ftlog.debug('MatchTableControllerDizhu.clearTable matchId=', table.matchInst.matchId,
                            'instId=', table.matchInst.instId,
                            'roomId=', table.roomId,
                            'tableId=', table.tableId,
                            'mo=', msg)
            router.sendTableServer(msg, table.roomId)
        except:
            ftlog.error()
    
class UserInfoLoaderDizhu(UserInfoLoader):
    def loadUserAttrs(self, userId, attrs):
        '''
        获取用户属性
        '''
        return userdata.getAttrs(userId, attrs)
    
    def getSessionClientId(self, userId):
        '''
        获取用户sessionClientId
        '''
        return sessiondata.getClientId(userId)

    def getGameSessionVersion(self, userId):
        '''
        获取用户所在Game插件版本
        '''
        return SessionDizhuVersion.getVersionNumber(userId) or 0
    
class MatchRankRewardsSenderDizhu(MatchRankRewardsSender):
    def __init__(self, room):
        self._room = room
        
    def sendRankRewards(self, player, rankRewards):
        '''
        给用户发奖
        '''
        conf = configure.getGameJson(DIZHU_GAMEID, 'wx.share.control', {})
        matchRewardSwitch = conf.get('matchRewardSwitch')
        if matchRewardSwitch:
            return
        try:
            ftlog.info('MatchRankRewardsSenderDizhu.sendRankRewards matchId=', player.matchInst.matchId,
                       'instId=', player.matchInst.instId,
                       'userId=', player.userId,
                       'rank=', player.rank,
                       'signinParams=', player.signinParams,
                       'rewards=', rankRewards.rewards)
            playerMixId = int(player.mixId) if player.mixId else None
            user_remote.addAssets(self._room.gameId, player.userId, rankRewards.rewards,
                                  'MATCH_REWARD', playerMixId or player.matchInst.matchId)

            from dizhu.game import TGDizhu
            from dizhu.entity.common.events import ActiveEvent
            TGDizhu.getEventBus().publishEvent(ActiveEvent(DIZHU_GAMEID, player.userId, 'redEnvelope'))

            if rankRewards:
                # 发邮件
                rewardDesc = matchutil.buildRewardsDesc(rankRewards)
                roomName = player.matchInst.matchConf.getRoomName(player.mixId)
                mailstr = '红包赛奖励#恭喜您在%s' % roomName + '中, 获得%s。' % rewardDesc
                message.send(DIZHU_GAMEID, message.MESSAGE_TYPE_SYSTEM, player.userId, mailstr)

            if rankRewards.message:
                pkmessage.sendPrivate(self._room.gameId, player.userId, 0, rankRewards.message)
                datachangenotify.sendDataChangeNotify(self._room.gameId, player.userId, 'message')

            sequence = int(player.matchInst.instId.split('.')[1])
            rewardsLen = len(rankRewards.rewards)
            for reward in rankRewards.rewards:
                # 如果是红包券则广播红包券事件
                if reward['itemId'] == 'user:coupon':
                    from hall.game import TGHall
                    TGHall.getEventBus().publishEvent(UserCouponReceiveEvent(HALL_GAMEID, player.userId, reward['count'], user_coupon_details.USER_COUPON_SOURCE_MATCH_ARENA))

                chipType = matchutil.getBiChipType(reward['itemId'])
                kindId = 0
                if chipType == daoconst.CHIP_TYPE_ITEM:
                    kindId = reward['itemId'].strip('item:')
                matchutil.report_bi_game_event('MATCH_REWARD', player.userId, player.matchInst.matchId, 0, sequence, 0, 0, 0,
                                               [chipType, reward['count'], kindId, player.rank, int(player.mixId) if player.mixId else 0, rewardsLen],
                                               'match_reward')
        except:
            ftlog.error()
    
class SigninFeeDizhu(SigninFee):
    def __init__(self, room):
        self._room = room
        
    def collectFee(self, inst, userId, fee, mixId=None):
        '''
        收取用户报名费
        '''
        if userId <= 10000:
            return

        # 月卡, 荣耀月卡报名比赛不扣除月卡
        from hall.entity import hallitem
        if fee.assetKindId in matchutil.getMatchSigninFeeWithoutCollect():
            userAssets = hallitem.itemSystem.loadUserAssets(userId)
            balance = userAssets.balance(HALL_GAMEID, fee.assetKindId, pktimestamp.getCurrentTimestamp())
            if balance:
                ftlog.info('SigninFeeDizhu.collectFee matchId=', inst.matchId,
                           'userId=', userId,
                           'fees=', fee,
                           'mixId=', mixId,
                           'signinParams=', {'mixId': mixId} if mixId else {},
                           'assetKindId=', fee.assetKindId,
                           'balance=', balance)
                return None
        contentItemList = []
        contentItemList.append({'itemId':fee.assetKindId, 'count':fee.count})
        from dizhu.games.matchutil import changeSignInFees
        contentItemList, changedItemId = changeSignInFees(userId, self._room.roomId, contentItemList)
        # 折扣报名判断， 如果有其他的折扣如折扣券，月卡等，执行这些
        if not changedItemId:
            contentItem = match_signin_discount.changeItemToDiscount(userId, mixId if mixId else self._room.bigRoomId, {'itemId':fee.assetKindId, 'count':fee.count})
            contentItemList = [contentItem]
            fee = copy.deepcopy(fee)
            fee.count = contentItem['count']
        assetKindId, count = user_remote.consumeAssets(self._room.gameId, userId, contentItemList,
                                                       'MATCH_SIGNIN_FEE', int(mixId) if mixId else None or self._room.roomId)

        ftlog.info('SigninFeeDizhu.collectFee matchId=', inst.matchId,
                   'userId=', userId,
                   'fees=', contentItemList,
                   'mixId=', mixId,
                   'signinParams=', {'mixId': mixId} if mixId else {},
                   'assetKindId=', assetKindId,
                   'count=', count)

        if assetKindId:
            raise SigninFeeNotEnoughException(inst, fee)
        return fee

    def returnFee(self, inst, userId, fee, mixId=None):
        '''
        退还报名费
        '''
        try:
            if userId <= 10000:
                return
            if not fee:
                return
            
            contentItemList = []
            contentItemList.append({'itemId':fee.assetKindId, 'count':fee.count})

            from dizhu.games.matchutil import returnSignInfFees
            contentItemList = returnSignInfFees(userId, self._room.roomId, contentItemList)

            user_remote.addAssets(self._room.gameId, userId, contentItemList, 'MATCH_RETURN_FEE', int(mixId) if mixId else None or self._room.roomId)
            ftlog.info('SigninFeeDizhu.returnFee matchId=', inst.matchId,
                       'userId=', userId,
                       'mixId=', mixId,
                       'signinParams=', {'mixId': mixId} if mixId else {},
                       'fees=', contentItemList)
        except:
            ftlog.error()


class MatchRankRewardsSelectorDizhu(MatchRankRewardsSelector):
    def __init__(self, room):
        self._room = room

    def getRewards(self, userId, rankRewards, mixId):
        '''
        获取渠道奖励
        '''
        ret = rankRewards
        if not ret:
            return ret
        clientId = sessiondata.getClientId(userId)
        try:
            rankRewardsChannelList = [MatchRankRewards().decodeFromDict(r) for r in
                                      dizhuhallinfo.getArenaMatchSessionRankRewards(userId, int(mixId) if mixId else self._room.bigRoomId, clientId)]
        except:
            return ret
        for rankRewardsChannel in rankRewardsChannelList:
            if rankRewards.startRank == rankRewardsChannel.startRank and rankRewards.endRank == rankRewardsChannel.endRank:
                ret = rankRewardsChannel
                break
        return ret

    def getRewardsList(self, userId, rankRewardsList, mixId):
        '''
        获取渠道奖励列表
        '''
        clientId = sessiondata.getClientId(userId)
        try:
            rankRewardsChannelList = [MatchRankRewards().decodeFromDict(r) for r in
                                      dizhuhallinfo.getArenaMatchSessionRankRewards(userId, int(mixId) if mixId else self._room.bigRoomId, clientId)]
        except:
            rankRewardsChannelList = []

        rankRewardsListCopy = copy.deepcopy(rankRewardsList)
        for index, rankRewards in enumerate(rankRewardsListCopy):
            for rankRewardsChannel in rankRewardsChannelList:
                if rankRewards.startRank == rankRewardsChannel.startRank and rankRewards.endRank == rankRewardsChannel.endRank:
                    rankRewardsListCopy[index] = rankRewardsChannel

        if ftlog.is_debug():
            ftlog.debug('MatchRankRewardsSelectorDizhu.getRewardsList userId=', userId,
                        'rankRewardsList=', rankRewardsList,
                        'rankRewardsChannelList=', rankRewardsChannelList,
                        'rankRewardsListCopy=', rankRewardsListCopy)

        return rankRewardsListCopy
            