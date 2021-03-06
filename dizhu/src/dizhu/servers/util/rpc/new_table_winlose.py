# -*- coding:utf-8 -*-
'''
Created on 2017年2月25日

@author: zhaojiangang
'''

import math

from dizhu.entity import dizhuconf, dizhutask
from dizhu.entity.dizhuconf import DIZHU_GAMEID
from dizhu.games import cardrules
from dizhu.servers.util.task_handler import TableTaskHelper
from dizhucomm.entity import treasurebox, skillscore
from dizhucomm.entity.events import Winlose, UserTableWinloseEvent
from dizhucomm.servers.util.rpc import comm_table_remote
import freetime.util.log as ftlog
from hall.entity import hallranking, hallstore, hallvip
from hall.entity.todotask import TodoTaskHelper
from hall.game import TGHall
from poker.entity.biz import bireport
from poker.entity.configure import gdata
from poker.entity.dao import gamedata, userchip, daoconst, sessiondata
from poker.entity.events.tyevent import GameOverEvent
from poker.protocol.rpccore import markRpcCall
from poker.util import strutil, timestamp as pktimestamp, city_locator
from poker.entity.events import hall51event


def _settlement(userId, roomId, tableId, roundId, clientId, deltaItems):
    # 把结算搞到UT执行
    final = 0
    totalTrueDelta = 0

    if deltaItems:
        for delta, eventId, eventParam in deltaItems:
            if delta != 0:
                trueDelta, final = userchip.incrTableChip(userId,
                                                          DIZHU_GAMEID,
                                                          delta,
                                                          daoconst.CHIP_NOT_ENOUGH_OP_MODE_CLEAR_ZERO,
                                                          eventId,
                                                          eventParam,
                                                          clientId,
                                                          tableId)
                totalTrueDelta += trueDelta
                ftlog.info('table_winlose._settlement',
                           'roomId=', roomId,
                           'tableId=', tableId,
                           'roundId=', roundId,
                           'userId=', userId,
                           'clientId=', clientId,
                           'eventId=', eventId,
                           'eventParam=', eventParam,
                           'delta=', delta,
                           'trueDelta=', trueDelta,
                           'final=', final)
        _recordLastTableChip(userId, final, True)
    else:
        final = userchip.getTableChip(userId, DIZHU_GAMEID, tableId)
    return final


def _settlementChip(userId, roomId, tableId, roundId, clientId, deltaItems):
    # 把结算搞到UT执行
    final = 0
    totalTrueDelta = 0

    if deltaItems:
        for delta, eventId, eventParam in deltaItems:
            if delta != 0:
                trueDelta, final = userchip.incrChip(userId,
                                                     DIZHU_GAMEID,
                                                     delta,
                                                     daoconst.CHIP_NOT_ENOUGH_OP_MODE_CLEAR_ZERO,
                                                     eventId,
                                                     eventParam,
                                                     clientId)
                totalTrueDelta += trueDelta
                ftlog.info('table_winlose._settlementChip',
                           'roomId=', roomId,
                           'tableId=', tableId,
                           'roundId=', roundId,
                           'userId=', userId,
                           'clientId=', clientId,
                           'eventId=', eventId,
                           'eventParam=', eventParam,
                           'delta=', delta,
                           'trueDelta=', trueDelta,
                           'final=', final)
    else:
        final = userchip.getChip(userId)
    return final


def buildSettlementDeltaItems(roomId, fee, cardNoteFee, winlose, winnerTax, fixedFee=0):
    items = []
    if fixedFee > 0:
        items.append((-fixedFee, 'ROOM_GAME_FIXED_FEE', roomId))
    if fee > 0:
        items.append((-fee, 'ROOM_GAME_FEE', roomId))
    if cardNoteFee > 0:
        items.append((-cardNoteFee, 'TABLE_OPEN_CARD_NOTE', roomId))
    if winlose != 0:
        items.append((winlose, 'GAME_WINLOSE', roomId))
    if winnerTax > 0:
        items.append((-winnerTax, 'WINNER_TAX', roomId))
    return items


@markRpcCall(groupName='userId', lockName='userId', syncCall=1)
def doFreeTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                         winUserId, isWin, winStreak, isDizhu, winloseScore,
                         finalScore, baseScore, winDoubles, bomb,
                         chuntian, winslam, playMode, topCardList):
    return _doFreeTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                                 winUserId, isWin, winStreak, isDizhu, winloseScore,
                                 finalScore, baseScore, winDoubles, bomb,
                                 chuntian, winslam, playMode, topCardList)


def _doFreeTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                          winUserId, isWin, winStreak, isDizhu, winloseScore,
                          finalScore, baseScore, winDoubles, bomb,
                          chuntian, winslam, playMode, topCardList):
    bigRoomId = strutil.getBigRoomIdFromInstanceRoomId(roomId)
    treasurebox.updateTreasureBoxWin(DIZHU_GAMEID, userId, bigRoomId)
    exp, deltaExp, _winrate = comm_table_remote.checkSetMedal(DIZHU_GAMEID, userId, baseScore, False, winloseScore)
    comm_table_remote.increaceChipTableWinrate(DIZHU_GAMEID, userId, False, winloseScore)
    skillScoreInfo = _caleSkillScoreByUser(userId, isWin, winStreak, isDizhu, roomId, winDoubles)
    skillLevelUp = skillScoreInfo.get('isLevelUp', False)
    # 广播用户结算事件
    card = cardrules.CARD_RULE_DICT[playMode]
    topValidCard = card.validateCards(topCardList, None)
    _publishWinLoseEvent(roomId, tableId, userId, roundNum, isWin, isDizhu, winUserId,
                         winloseScore, finalScore, winDoubles, bomb, chuntian,
                         winslam, clientId, topValidCard, skillLevelUp)
    finalChip = userchip.getChip(userId)
    return finalChip, [exp, deltaExp], skillScoreInfo


@markRpcCall(groupName='userId', lockName='userId', syncCall=1)
def doTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                     isWin, winStreak, winUserId, isDizhu,
                     fee, cardNoteFee, winlose, systemPaid, winnerTax,
                     baseScore, winDoubles, bomb, chuntian,
                     winslam, playMode, topCardList, **kwargs):
    return _doTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                             isWin, winStreak, winUserId, isDizhu,
                             fee, cardNoteFee, winlose, systemPaid, winnerTax,
                             baseScore, winDoubles, bomb, chuntian,
                             winslam, playMode, topCardList, **kwargs)


def _reportRoomDelta(userId, roomId, bigRoomId, clientId, systemPaid):
    if ftlog.is_debug():
        ftlog.debug('new_table_winlose._reportRoomDelta',
                    'userId=', userId,
                    'roomId=', roomId,
                    'bigRoomId=', bigRoomId,
                    'clientId=', clientId,
                    'systemPaid=', systemPaid)
    if systemPaid == 0:
        return
    try:
        clientId, clientIdNum = sessiondata.getClientIdNum(userId, clientId)
        args = {}
        args['clientId'] = clientId
        args['appId'] = DIZHU_GAMEID
        args['deltaCount'] = systemPaid
        args['lowLimit'] = -1
        args['highLimit'] = -1
        args['chipType'] = daoconst.CHIP_TYPE_TABLE_CHIP
        args['mode'] = daoconst.CHIP_NOT_ENOUGH_OP_MODE_NONE
        eventId, delta = ('DDZ_ROOM_PAID', systemPaid) if systemPaid > 0 else ('DDZ_ROOM_RECOVERY', systemPaid)
        bireport.reportBiChip(userId, delta, delta, 0, eventId,
                              clientIdNum, DIZHU_GAMEID, DIZHU_GAMEID, bigRoomId,
                              daoconst.CHIP_TYPE_TABLE_CHIP, argdict=args)
    except:
        ftlog.warn('new_table_winlose._reportRoomDelta',
                   'userId=', userId,
                   'roomId=', roomId,
                   'bigRoomId=', bigRoomId,
                   'clientId=', clientId,
                   'systemPaid=', systemPaid)


def _doTableWinloseUT(userId, roomId, tableId, roundNum, clientId,
                      isWin, winStreak, winUserId, isDizhu,
                      fee, cardNoteFee, winlose, systemPaid, winnerTax,
                      baseScore, winDoubles, bomb, chuntian,
                      winslam, playMode, topCardList, **kwargs):
    bigRoomId = strutil.getBigRoomIdFromInstanceRoomId(roomId)
    treasurebox.updateTreasureBoxWin(DIZHU_GAMEID, userId, kwargs.get('mixConfRoomId') or bigRoomId)
    exp, deltaExp, _winrate = comm_table_remote.checkSetMedal(DIZHU_GAMEID, userId, baseScore, False, winlose)
    comm_table_remote.increaceChipTableWinrate(DIZHU_GAMEID, userId, False, winlose)
    fixedFee = kwargs.get('fixedFee', 0)
    deltaItems = buildSettlementDeltaItems(kwargs.get('mixConfRoomId') or roomId, fee, cardNoteFee, winlose, winnerTax,
                                           fixedFee=fixedFee)
    skillScoreInfo = _caleSkillScoreByUser(userId, isWin, winStreak, isDizhu, kwargs.get('mixConfRoomId') or bigRoomId,
                                           winDoubles)
    skillLevelUp = skillScoreInfo.get('isLevelUp', False)

    _reportRoomDelta(userId, roomId, bigRoomId, clientId, systemPaid)
    finalTableChip = _settlement(userId, kwargs.get('mixConfRoomId') or roomId, tableId, roundNum, clientId, deltaItems)

    # 纪录连胜
    if isWin:
        # 连胜＋1
        temp_winstreak = gamedata.incrGameAttr(userId, DIZHU_GAMEID, 'winstreak', 1)
        # 终止连胜清零
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'stopwinstreak', 0)
        # 更新lastwinstreak
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'lastwinstreak', temp_winstreak)
        # 检查是否需要更新历史最佳连胜maxwinstreak
        temp_maxwinstreak = gamedata.getGameAttr(userId, DIZHU_GAMEID, 'maxwinstreak') or 0
        if temp_winstreak > temp_maxwinstreak:
            gamedata.setGameAttr(userId, DIZHU_GAMEID, 'maxwinstreak', temp_winstreak)
        # 连败清零
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'losestreak', 0)

    else:
        userWinStreak = gamedata.getGameAttrInt(userId, DIZHU_GAMEID, 'winstreak')
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'stopwinstreak', userWinStreak)
        # 连胜清零
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'winstreak', 0)
        # 连败+1
        gamedata.incrGameAttr(userId, DIZHU_GAMEID, 'losestreak', 1)

    if ftlog.is_debug():
        winstreaklog = gamedata.getGameAttr(userId, DIZHU_GAMEID, 'winstreak')
        losestreaklog = gamedata.getGameAttr(userId, DIZHU_GAMEID, 'losestreak')
        ftlog.debug('_doTableWinloseUT winstreak=', winstreaklog,
                    'losestreak=', losestreaklog,
                    'UserID=', userId,
                    'roomId=', roomId,
                    'tableId=', tableId,
                    'isWin=', isWin)

    # 广播用户结算事件
    card = cardrules.CARD_RULE_DICT[playMode]
    topValidCard = card.validateCards(topCardList, None)
    finalUserChip = userchip.getChip(userId)
    _publishWinLoseEvent(roomId, tableId, userId, roundNum, isWin, isDizhu, winUserId,
                         winlose, finalTableChip, winDoubles, bomb, chuntian,
                         winslam, clientId, topValidCard, skillLevelUp, baseScore, **kwargs)
    # 更新排名相关数据
    _calRankInfoData(userId, winlose, winslam, winDoubles)

    return finalTableChip, finalUserChip, [exp, deltaExp], skillScoreInfo


def _publishWinLoseEvent(roomId, tableId, seatUid, roundId, isWin, isDizhu, winuserid, seat_delta,
                         findChips, windoubles, bomb, chuntian, winslam, clientId, topValidCard, skillLevelUp,
                         baseScore, **kwargs):
    from dizhu.game import TGDizhu
    ebus = TGDizhu.getEventBus()
    hallebus = TGHall.getEventBus()
    assist = kwargs.get('assist', 0)
    validMaxOutCard = kwargs.get('validMaxOutCard', 0)
    outCardSeconds = kwargs.get('outCardSeconds', 0)
    winlose = Winlose(winuserid, topValidCard, isWin, isDizhu, seat_delta, findChips,
                      windoubles, bomb, chuntian > 1, winslam, baseScore, outCardSeconds=outCardSeconds, assist=assist, validMaxOutCard=validMaxOutCard)
    mixConfRoomId = kwargs.get('mixConfRoomId')
    ebus.publishEvent(UserTableWinloseEvent(DIZHU_GAMEID, seatUid, roomId, tableId, winlose, skillLevelUp,
                                            mixConfRoomId=mixConfRoomId))
    isWinNum = 0
    if isWin:
        isWinNum = 1
    roomLevel = gdata.roomIdDefineMap()[roomId].configure.get('roomLevel', 1)
    ftlog.debug('hallebus push gameoverevent userId=', seatUid)

    roomId = mixConfRoomId if mixConfRoomId else roomId
    hallebus.publishEvent(GameOverEvent(seatUid, DIZHU_GAMEID, clientId, roomId, tableId, isWinNum, roomLevel))
    tempGameResult = 1 if isWin else -1
    tempWinstreak = gamedata.getGameAttr(seatUid, DIZHU_GAMEID, 'winstreak')
    hall51event.sendToHall51GameOverEvent(seatUid, DIZHU_GAMEID, roomId, tableId, tempGameResult, roomLevel, roundId,
                                          tempWinstreak)


@markRpcCall(groupName="userId", lockName="userId", syncCall=1)
def queryDataAfterWinlose(userId, bigRoomId, isWin, winStreak, slam, isChuntian, clientId):
    tbplaytimes, tbplaycount = treasurebox.getTreasureBoxState(DIZHU_GAMEID, userId, bigRoomId)
    luckyArgs = _caleLuckyItemArgsByUser(userId, bigRoomId, isWin, winStreak, slam, isChuntian, clientId)
    tasks = _queryUserTableTasks(userId, clientId)
    return {
        'tbbox': [tbplaytimes, tbplaycount],
        'luckyArgs': luckyArgs,
        'tableTasks': tasks
    }


def _recordLastTableChip(userId, tablechip, isSupportBuyin):
    if isSupportBuyin:
        gamedata.setGameAttr(userId, DIZHU_GAMEID, 'last_table_chip', tablechip)


def _caleLuckyItemArgsByUser(userId, bigRoomId, isWin, winStreak, slam, isChuntian, clientId):
    if ftlog.is_debug():
        ftlog.debug('table_remote.caleLuckyItemArgs',
                    userId,
                    bigRoomId,
                    isWin,
                    winStreak,
                    slam,
                    isChuntian,
                    clientId)
    lucky_args = {}
    if isWin:
        conf = dizhuconf.getRoomWinLosePayInfo(bigRoomId, clientId)
        if not conf:
            conf = {}
        payOrder = None
        if isChuntian and ('spring' in conf):
            payOrder = conf['spring']
        if winStreak >= 3 and ('winstreak' in conf):
            payOrder = conf['winstreak']
        if slam == 1 and ('slam' in conf):
            payOrder = conf['slam']
        if payOrder:
            product, _ = hallstore.findProductByPayOrder(DIZHU_GAMEID, userId, clientId, payOrder)
            if product:
                lucky_args = TodoTaskHelper.getParamsByProduct(product)
    return lucky_args


def _get_room_ratio(bigRoomId):
    confs = dizhuconf.getSkillSCoreRatioRoom()
    for conf_ in confs:
        if bigRoomId in conf_['roomlist']:
            return conf_.get('ratio', 1)
    return 1


def _calcToAddSkillScore(userId, bigRoomId, isDizhu, isWin, winStreak, windoubles):
    privilege_rat = 1
    room_rat = _get_room_ratio(bigRoomId)
    mult_rat = int(math.ceil(windoubles / float(40)))
    dizhu_rat = 1.5 if isDizhu else 1
    winstr_rat = int(math.ceil(winStreak / float(4)))
    userVip = hallvip.userVipSystem.getUserVip(userId)
    score = int(privilege_rat * room_rat * mult_rat * dizhu_rat * winstr_rat)

    # 订阅会员增加大师分翻倍
    from hall.entity import hallitem
    from poker.util import timestamp
    remainDays, _ = hallitem.getMemberInfo(userId, timestamp.getCurrentTimestamp())
    if remainDays > 0:
        if ftlog.is_debug():
            ftlog.debug('skillscore.inc_skill_score remainDays=', remainDays,
                        'add_score=', score, 'toDouble=', score * 2)
        score *= 2
        return score

    return vipDaShiFen(score, userVip.vipLevel.level)


def vipDaShiFen(score, vipLevel):
    """
    vipLevel在confLevel级以上 大师分加成rate
    :param score:
    :param vipLevel:
    :return:
    """
    result = score
    confs = dizhuconf.getVipSpecialRight().get('dashifen', [])
    for conf in confs:
        confLevel = conf.get('level', -1)
        if vipLevel >= confLevel:
            rate = conf.get('rate', 0)
            if isinstance(rate, (int, float)):
                result = score + score * rate
    return int(result)


def _caleSkillScoreByUser(userId, isWin, winStreak, isDizhu, bigRoomId, windoubles):
    toAdd = _calcToAddSkillScore(userId, bigRoomId, isDizhu, isWin, winStreak, windoubles)
    skill_score_infos = skillscore.addUserSkillScore(DIZHU_GAMEID, userId, toAdd)
    if ftlog.is_debug():
        ftlog.debug('table_winlose._caleSkillScoreByUser',
                    'userId=', userId,
                    'infos=', skill_score_infos)
    return skill_score_infos


def _calRankInfoData(userId, seatDeltaChip, winslam, windoubles):
    ftlog.debug('calRankInfoData->', userId, seatDeltaChip, winslam, windoubles)
    # 每周,城市赢金榜
    if seatDeltaChip > 0 and dizhuconf.isUseTuyouRanking():  # 陌陌使用自己的排行榜
        city_code = sessiondata.getCityZip(userId)
        city_index = city_locator.ZIP_CODE_INDEX.get(city_code, 1)
        rankingId = 110006100 + city_index
        hallranking.rankingSystem.setUserScore(str(rankingId), userId, seatDeltaChip)

    # 更新gamedata中的各种max和累积值
    winchips, losechips, maxwinchip, weekchips, winrate, maxweekdoubles, slams, todaychips = gamedata.getGameAttrs(
        userId, DIZHU_GAMEID,
        ['winchips', 'losechips', 'maxwinchip',
         'weekchips', 'winrate', 'maxweekdoubles',
         'slams', 'todaychips'],
        False)
    ftlog.debug('calRankInfoData->', winchips, losechips, maxwinchip, weekchips, winrate, maxweekdoubles, slams)
    winchips, losechips, maxwinchip, maxweekdoubles, slams = strutil.parseInts(winchips, losechips, maxwinchip,
                                                                               maxweekdoubles, slams)
    updatekeys = []
    updatevalues = []

    # 计算累计的输赢金币
    if seatDeltaChip > 0:
        winchips = winchips + seatDeltaChip
        updatekeys.append('winchips')
        updatevalues.append(winchips)
    else:
        losechips = losechips + seatDeltaChip
        updatekeys.append('losechips')
        updatevalues.append(losechips)

    # 计算增加最大的赢取金币数
    if seatDeltaChip > maxwinchip:
        updatekeys.append('maxwinchip')
        updatevalues.append(seatDeltaChip)

    # 计算增加每星期的累计赢取、输掉的金币数
    weekchips = strutil.loads(weekchips, ignoreException=True)
    if weekchips == None or len(weekchips) != 2 or (not 'week' in weekchips) or (not 'chips' in weekchips):
        weekchips = {'week': -1, 'chips': 0}
    weekOfYear = pktimestamp.formatTimeWeekInt()
    if weekOfYear != weekchips['week']:
        weekchips = {'week': weekOfYear, 'chips': seatDeltaChip}
    else:
        weekchips['chips'] = weekchips['chips'] + seatDeltaChip
    updatekeys.append('weekchips')
    updatevalues.append(strutil.dumps(weekchips))

    # 计算增加每星期的累计赢取的金币数
    if seatDeltaChip > 0:
        todaychips = strutil.loads(todaychips, ignoreException=True)
        if todaychips == None or len(todaychips) != 3 \
                or (not 'today' in todaychips) or (not 'chips' in todaychips) or (not 'last' in todaychips):
            todaychips = {'today': -1, 'chips': 0, 'last': 0}
        today = pktimestamp.formatTimeDayInt()
        if today != todaychips['today']:
            yesterdaychips = 0
            if todaychips['today'] == pktimestamp.formatTimeYesterDayInt():
                yesterdaychips = todaychips['chips']
            todaychips = {'today': today, 'chips': seatDeltaChip, 'last': yesterdaychips}
        else:
            todaychips['chips'] = todaychips['chips'] + seatDeltaChip
        updatekeys.append('todaychips')
        updatevalues.append(strutil.dumps(todaychips))

    # 计算marsscore
    winchipsDelta = int(winchips) - int(losechips)
    winrate = strutil.loads(winrate, ignoreException=True, execptionValue={'wt': 0})
    wintimes = int(winrate.get('wt', 0))
    marsscore = winchipsDelta * 0.6 + wintimes * 200 * 0.4
    if marsscore > 0:
        updatekeys.append('marsscore')
        updatevalues.append(marsscore)

    # 计算slams和maxweekdoubles
    if seatDeltaChip > 0:
        if winslam:
            updatekeys.append('slams')
            updatevalues.append(slams + 1)

        if maxweekdoubles < windoubles:
            updatekeys.append('maxweekdoubles')
            updatevalues.append(windoubles)

    if len(updatekeys) > 0:
        gamedata.setGameAttrs(userId, DIZHU_GAMEID, updatekeys, updatevalues)
    return 1


def _queryUserTableTasks(userId, clientId):
    taskModel = dizhutask.tableTaskSystem.loadTaskModel(userId, pktimestamp.getCurrentTimestamp())
    return TableTaskHelper.buildTableTasks(taskModel)


