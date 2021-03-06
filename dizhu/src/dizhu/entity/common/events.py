# -*- coding:utf-8 -*-
'''
Created on 2018年8月21日

@author: wangyonghui
'''

from poker.entity.events.tyevent import UserEvent


class UserShareLoginEvent(UserEvent):
    '''
    分享用户登录事件
    '''
    def __init__(self, gameId, userId, shareUserId, **kwargs):
        super(UserShareLoginEvent, self).__init__(userId, gameId)
        self.shareUserId = shareUserId
        self.params = kwargs


class ActiveEvent(UserEvent):
    '''
    用户活跃度事件
    '''

    def __init__(self, gameId, userId, eventId, **kwargs):
        super(ActiveEvent, self).__init__(userId, gameId)
        self.eventId = eventId
        self.kwargs = kwargs