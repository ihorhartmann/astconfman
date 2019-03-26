import logging
import time
from os.path import dirname, join
from datetime import datetime
from threading import Thread

from flask_babelex import gettext, lazy_gettext
from datetime import datetime
from sqlalchemy.ext.hybrid import hybrid_property
from flask_sqlalchemy import before_models_committed
from sqlalchemy.orm import session

import asterisk
from crontab import CronTab

import config
from app import app, db, sse_notify

logging.basicConfig(filename='astconfman.log', level=logging.DEBUG)


class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Unicode(128), index=True)
    phone = db.Column(db.String(32))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='contacts')
    superior_id = db.Column(db.Integer, db.ForeignKey(id), nullable=True)
    superior = db.relationship('Contact', remote_side=id)
    subordinates = db.relationship('Contact', remote_side=superior_id, uselist=True)

    def __unicode__(self):
        if self.name:
            return '%s <%s>' % (self.name, self.phone)
        else:
            return self.phone


class Conference(db.Model):
    """Conference is an event held in in a Room"""
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(16), unique=True)
    name = db.Column(db.Unicode(128))
    is_public = db.Column(db.Boolean)
    conference_profile_id = db.Column(db.Integer,
                                      db.ForeignKey('conference_profile.id'))
    conference_profile = db.relationship('ConferenceProfile')
    public_participant_profile_id = db.Column(
        db.Integer,
        db.ForeignKey('participant_profile.id'))
    public_participant_profile = db.relationship('ParticipantProfile')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='conferences')

    def __str__(self):
        return '%s <%s>' % (self.name, self.number)

    def _online_participant_count(self):
        return asterisk.confbridge_get_user_count(self.number) or 0

    online_participant_count = property(_online_participant_count)

    def _invited_participant_count(self):
        return Participant.query.filter_by(conference=self, is_invited=True).count()

    invited_participant_count = property(_invited_participant_count)

    def _participant_count(self):
        return len(self.participants)

    participant_count = property(_participant_count)

    def _is_locked(self):
        return asterisk.confbridge_is_locked(self.number)

    is_locked = property(_is_locked)

    def log(self, message):
        post = ConferenceLog(conference=self, message=message)
        db.session.add(post)
        db.session.commit()
        sse_notify(self.id, 'log_message', message)

    def invite_participants(self):
        logging.debug("INVITE PARTICIPANTS")
        online_participants = [
            k['callerid'] for k in asterisk.confbridge_list_participants(
                self.number)]
        gen = (p for p in self.participants if p.is_invited and p.phone \
               not in online_participants)
        for p in gen:
            self._invite_user(self.number, p.phone, name=p.name,
                              bridge_options=self.conference_profile.get_confbridge_options(),
                              user_options=p.profile.get_confbridge_options())

    def invite_guest(self, phone):
        logging.debug('INVITE GUEST ' + str(phone))
        self._invite_user(self.number, phone,
                          bridge_options=self.conference_profile.get_confbridge_options(),
                          user_options=self.public_participant_profile.get_confbridge_options())

    def _invite_user(self, confnum, number, name='', bridge_options=[], user_options=[]):
        logging.debug('INVITE USER ' + str(number))
        asterisk.originate(confnum, number, name=name,
                           bridge_options=bridge_options,
                           user_options=user_options
                           )
        thread = Thread(target=self._waiting_for_answer,
                        args=(confnum, number, name,
                              bridge_options,
                              user_options))
        thread.start()

    # Be carefully: this method called from different threads
    def _waiting_for_answer(self, confnum, number, name='', bridge_options=[], user_options=[]):
        logging.debug('WAITING FOR ANSWER ' + str(number))
        if self._if_call_will_be_redirected(number):
            with app.app_context():
                db.init_app(app)
                contact = Contact.query.filter(Contact.phone == number).first()
                for subordinate in contact.subordinates:
                    logging.debug('USER ' + str(number) + ' REDIRECTED TO ' + str(subordinate.phone))
                    self._invite_user(confnum, subordinate.phone,
                                      bridge_options=bridge_options,
                                      user_options=user_options)

    def _if_call_will_be_redirected(self, phone):
        logging.debug('IF CALL WILL BE REDIRECTED FOR ' + str(phone))
        with app.app_context():
            db.init_app(app)
            contact_id = Contact.query.filter(Contact.phone == phone).first().id
            if Contact.query.filter(Contact.superior_id == contact_id).count() == 0:
                logging.debug('NO REDIRECT NO SUBORDINATE FOR ' + str(phone))
                return False
        status_is_up = False
        status_up = 'Up'
        start_time = time.time()
        time.sleep(0.2 * config.SECONDS_BEFORE_REDIRECT)
        while True:
            lines = asterisk.get_all_channels().splitlines()
            del lines[0]
            del lines[len(lines) - 3: len(lines)]
            channels = {}
            for line in lines:
                if line.split()[0].__contains__(phone.__str__()) \
                        and (line.split()[3].__contains__('ConfBridge(' + self.number.__str__() + ')')
                             or line.split()[3].__contains__('AppDial2((Outgoing')):
                    channels[line.split()[0]] = line.split()[2]
            for channel in channels:
                if channels[channel] == status_up:
                    status_is_up = True
                    break
            if status_is_up:
                logging.debug('NO REDIRECT STATUS IS UP FOR ' + str(phone))
                return False
            elif len(channels) == 0 or time.time() - start_time > config.SECONDS_BEFORE_REDIRECT:
                logging.debug('REDIRECT FOR ' + str(phone))
                return True
            time.sleep(1)


class ConferenceLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    added = db.Column(db.DateTime, default=datetime.now)
    message = db.Column(db.Unicode(1024))
    conference_id = db.Column(db.Integer, db.ForeignKey('conference.id'))
    conference = db.relationship('Conference', backref='logs')

    def __str__(self):
        return '%s: %s' % (self.added, self.message)


class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(32), index=True)
    name = db.Column(db.Unicode(128))
    is_invited = db.Column(db.Boolean, default=True)
    conference_id = db.Column(db.Integer, db.ForeignKey('conference.id'))
    conference = db.relationship('Conference',
                                 backref=db.backref(
                                     'participants', ))
    # cascade="delete,delete-orphan"))
    profile_id = db.Column(db.Integer, db.ForeignKey('participant_profile.id'))
    profile = db.relationship('ParticipantProfile')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='participants')

    __table_args__ = (db.UniqueConstraint('conference_id', 'phone',
                                          name='uniq_phone'),)

    def __str__(self):
        if self.name:
            return '%s <%s>' % (self.name, self.phone)
        else:
            return self.phone


class ConferenceProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Unicode(128))
    max_members = db.Column(db.Integer, default=50)
    record_conference = db.Column(db.Boolean)
    internal_sample_rate = db.Column(db.String(8))
    mixing_interval = db.Column(db.String(2), default='20')
    video_mode = db.Column(db.String(16))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='conference_profiles')

    def __str__(self):
        return self.name

    def get_confbridge_options(self):
        options = []
        if self.max_members:
            options.append('max_members=%s' % self.max_members)
        if self.record_conference:
            options.append('record_conference=yes')
        if self.internal_sample_rate:
            options.append(
                'internal_sample_rate=%s' % self.internal_sample_rate)
        if self.mixing_interval:
            options.append('mixing_interval=%s' % self.mixing_interval)
        if self.video_mode:
            options.append('video_mode=%s' % self.video_mode)

        return options


class ParticipantProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Unicode(128))
    admin = db.Column(db.Boolean, index=True)
    marked = db.Column(db.Boolean, index=True)
    startmuted = db.Column(db.Boolean)
    music_on_hold_when_empty = db.Column(db.Boolean)
    music_on_hold_class = db.Column(db.String(64), default='default')
    quiet = db.Column(db.Boolean)
    announce_user_count = db.Column(db.Boolean)
    announce_user_count_all = db.Column(db.String(4))
    announce_only_user = db.Column(db.Boolean)
    announcement = db.Column(db.String(128))
    wait_marked = db.Column(db.Boolean)
    end_marked = db.Column(db.Boolean)
    dsp_drop_silence = db.Column(db.Boolean)
    dsp_talking_threshold = db.Column(db.Integer, default=160)
    dsp_silence_threshold = db.Column(db.Integer, default=2500)
    talk_detection_events = db.Column(db.Boolean)
    denoise = db.Column(db.Boolean)
    jitterbuffer = db.Column(db.Boolean)
    pin = db.Column(db.String, index=True)
    announce_join_leave = db.Column(db.Boolean)
    dtmf_passthrough = db.Column(db.Boolean)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='participant_profiles')

    def __str__(self):
        return self.name

    def get_confbridge_options(self):
        options = []
        if self.admin:
            options.append('admin=yes')
        if self.marked:
            options.append('marked=yes')
        if self.startmuted:
            options.append('startmuted=yes')
        if self.music_on_hold_when_empty:
            options.append('music_on_hold_when_empty=yes')
        if self.music_on_hold_class:
            options.append('music_on_hold_class=%s' % self.music_on_hold_class)
        if self.quiet:
            options.append('quiet=yes')
        if self.announce_user_count:
            options.append('announce_user_count=yes')
        if self.announce_user_count_all:
            options.append(
                'announce_user_count_all=%s' % self.announce_user_count_all)
        if self.announce_only_user:
            options.append('announce_only_user=yes')
        if self.announcement:
            options.append('announcement=%s' % self.announcement)
        if self.wait_marked:
            options.append('wait_marked=yes')
        if self.end_marked:
            options.append('end_marked=yes')
        if self.dsp_drop_silence:
            options.append('dsp_drop_silence=yes')
        if self.dsp_talking_threshold:
            options.append(
                'dsp_talking_threshold=%s' % self.dsp_talking_threshold)
        if self.dsp_silence_threshold:
            options.append(
                'dsp_silence_threshold=%s' % self.dsp_silence_threshold)
        if self.talk_detection_events:
            options.append('talk_detection_events=yes')
        if self.denoise:
            options.append('denoise=yes')
        if self.jitterbuffer:
            options.append('jitterbuffer=yes')
        if self.pin:
            options.append('pin=%s' % self.pin)
        if self.announce_join_leave:
            options.append('announce_join_leave=yes')
        if self.dtmf_passthrough:
            options.append('dtmf_passthrough=yes')

        return options


class ConferenceSchedule(db.Model):
    """
    This is a model to keep planned conferences in crontab format.
    """
    id = db.Column(db.Integer, primary_key=True)
    conference_id = db.Column(db.Integer, db.ForeignKey('conference.id'))
    conference = db.relationship('Conference')
    entry = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='schedules')

    # May be will refactor :-)
    # minute = db.Column(db.String(64))
    # hour = db.Column(db.String(64))
    # day_of_month = db.Column(db.String(64))
    # month = db.Column(db.String(64))
    # day_of_week = db.Column(db.String(64))

    def __str__(self):
        return self.entry
