import logging
import random
import re
from django.db import models
from django.db.models import signals
from django.conf import settings
from django.core.exceptions import ValidationError, ObjectDoesNotExist, MultipleObjectsReturned
from django.core.cache import cache
from django.utils.functional import cached_property

from debate.adjudicator.anneal import SAAllocator
from debate.result import BallotSet
from debate.draw import DrawGenerator, DrawError, DRAW_FLAG_DESCRIPTIONS

import standings

from warnings import warn
from threading import BoundedSemaphore
from collections import OrderedDict

logger = logging.getLogger(__name__)


class Tournament(models.Model):
    name = models.CharField(max_length=100, help_text="The full name used on the homepage")
    short_name  = models.CharField(max_length=25, blank=True, null=True, default="", help_text="The name used in the menu")
    seq = models.IntegerField(db_index=True, blank=True, null=True, help_text="The order in which tournaments are displayed")
    slug = models.SlugField(unique=True, db_index=True, help_text="The sub-URL of the tournament; cannot have spaces")
    current_round = models.ForeignKey('Round', null=True, blank=True,
                                     related_name='tournament_', help_text="Must be set for the tournament to start! (Set after rounds are inputted)")
    welcome_msg = models.TextField(blank=True, null=True, default="", help_text="Text/html entered here shows on the homepage")
    release_all = models.BooleanField(default=False, help_text="This releases all results; do so only after the tournament is finished")
    active = models.BooleanField(default=True)

    @property
    def LAST_SUBSTANTIVE_POSITION(self):
        """Returns the number of substantive speakers."""
        return self.config.get('substantive_speakers')

    @property
    def REPLY_POSITION(self):
        """If there is a reply position, returns one more than the number of
        substantive speakers. If there is no reply position, returns None."""
        if self.config.get('reply_scores_enabled'):
            return self.config.get('substantive_speakers') + 1
        else:
            return None

    @property
    def POSITIONS(self):
        """Guaranteed to be consecutive numbers starting at one. Includes the
        reply speaker."""
        speaker_positions = 1 + self.config.get('substantive_speakers')
        if self.config.get('reply_scores_enabled') is True:
            speaker_positions = speaker_positions + 1
        return range(1, speaker_positions)

    @models.permalink
    def get_absolute_url(self):
        return ('tournament_home', [self.slug])

    @models.permalink
    def get_public_url(self):
        return ('public_index', [self.slug])

    @models.permalink
    def get_all_tournaments_all_venues(self):
        return ('all_tournaments_all_venues', [self.slug])

    @models.permalink
    def get_all_tournaments_all_institutions(self):
        return ('all_tournaments_all_institutions', [self.slug])

    @models.permalink
    def get_all_tournaments_all_teams(self):
        return ('all_tournaments_all_teams', [self.slug])

    @property
    def teams(self):
        return Team.objects.filter(tournament=self)

    @cached_property
    def get_current_round_cached(self):
        cached_key = "%s_current_round_object" % self.slug
        cached_value = cache.get(cached_key)
        if cached_value:
            return cache.get(cached_key)
        else:
            cache.set(cached_key, self.current_round, None)
            return self.current_round

    def prelim_rounds(self, before=None, until=None):
        qs = Round.objects.filter(stage=Round.STAGE_PRELIMINARY, tournament=self)
        if until:
            qs = qs.filter(seq__lte=until.seq)
        if before:
            qs = qs.filter(seq__lt=before.seq)
        return qs

    def create_next_round(self):
        curr = self.current_round
        next = curr.seq + 1
        r = Round(name="Round %d" % next, seq=next, type=Round.DRAW_POWERPAIRED,
                  tournament=self)
        r.save()
        r.activate_all()

    def advance_round(self):
        next_round_seq = self.current_round.seq + 1
        next_round = Round.objects.get(seq=next_round_seq, tournament=self)
        self.current_round = next_round
        self.save()

    @cached_property
    def config(self):
        if not hasattr(self, '_config'):
            from options.options import Config # TODO: improve the semantics here
            self._config = Config(self)
        return self._config

    @cached_property
    def adj_feedback_questions(self):
        return self.adjudicatorfeedbackquestion_set.order_by("seq")

    class Meta:
        ordering = ['seq',]

    def __unicode__(self):
        if self.short_name:
            return unicode(self.short_name)
        else:
            return unicode(self.name)

def update_tournament_cache(sender, instance, created, **kwargs):
    cached_key = "%s_%s" % (instance.slug, 'object')
    cache.delete(cached_key)
    cached_key = "%s_%s" % (instance.slug, 'current_round_object')
    cache.delete(cached_key)

# Update the cached tournament object when model is changed)
signals.post_save.connect(update_tournament_cache, sender=Tournament)



class Region(models.Model):
    name = models.CharField(db_index=True, max_length=100)
    tournament = models.ForeignKey(Tournament)

    def __unicode__(self):
        return u'%s' % (self.name)


class InstitutionManager(models.Manager):

    def lookup(self, name, **kwargs):
        """Queries for an institution with matching name in any of the three
        name fields."""
        for field in ('code', 'name', 'abbreviation'):
            try:
                kwargs[field] = name
                return self.get(**kwargs)
            except ObjectDoesNotExist:
                kwargs.pop(field)
        raise self.model.DoesNotExist("No institution matching '%s'" % name)


class Institution(models.Model):
    name = models.CharField(db_index=True, max_length=100, help_text="The institution's full name, e.g., \"University of Cambridge\", \"Victoria University of Wellington\"")
    code = models.CharField(max_length=20, help_text="What the institution is typically called for short, e.g., \"Cambridge\", \"Vic Wellington\"")
    abbreviation = models.CharField(max_length=8, default="", help_text="For extremely confined spaces, e.g., \"Camb\", \"VicWgtn\"")
    region = models.ForeignKey(Region, blank=True, null=True)

    objects = InstitutionManager()

    class Meta:
        unique_together = [('name', 'code')]
        ordering = ['name']

    def __unicode__(self):
        return unicode(self.name)

    @property
    def short_code(self):
        if self.abbreviation:
            return self.abbreviation
        else:
            return self.code[:5]

class TeamManager(models.Manager):

    def get_queryset(self):
        return super(TeamManager, self).get_queryset().select_related('institution')

    def lookup(self, name, **kwargs):
        """Queries for a team with a matching name."""
        # TODO could be improved to take in a better range of fields
        try:
            institution_name, reference = name.rsplit(None, 1)
        except:
            print "Error in", repr(name)
            raise
        institution_name = institution_name.strip()
        institution = Institution.objects.lookup(institution_name)
        return self.get(institution=institution, reference=reference, **kwargs)

    def _teams_for_standings(self, round):
        return self.filter(debateteam__debate__round__seq__lte=round.seq,
            tournament=round.tournament).select_related('institution')

    def standings(self, round):
        """Returns a list."""
        teams = self._teams_for_standings(round)
        return standings.annotate_team_standings(teams, round)

    def ranked_standings(self, round):
        """Returns a list."""
        teams = self._teams_for_standings(round)
        return standings.ranked_team_standings(teams, round)

    def division_standings(self, round):
        """Returns a list."""
        teams = self._teams_for_standings(round)
        return standings.division_ranked_team_standings(teams, round)

    def subrank_standings(self, round):
        """Returns a list."""
        teams = self._teams_for_standings(round)
        return standings.subranked_team_standings(teams, round)


class Division(models.Model):
    name = models.CharField(max_length=50, verbose_name="Name or suffix")
    seq = models.IntegerField(blank=True, null=True, help_text="The order in which divisions are displayed")
    tournament = models.ForeignKey(Tournament)
    time_slot = models.TimeField(blank=True, null=True)
    venue_group = models.ForeignKey('venues.VenueGroup', blank=True, null=True)

    @property
    def teams_count(self):
        return self.team_set.count()

    @cached_property
    def teams(self):
        return self.team_set.all().order_by('institution','reference').select_related('institution')

    def __unicode__(self):
        return u"%s - %s" % (self.tournament, self.name)

    class Meta:
        unique_together = [('tournament', 'name')]
        ordering = ['tournament', 'seq']
        index_together = ['tournament', 'seq']


class Team(models.Model):
    reference = models.CharField(max_length=150, verbose_name="Full name or suffix", help_text="Do not include institution name (see \"uses institutional prefix\" below)")
    short_reference = models.CharField(max_length=35, verbose_name="Short name/suffix", help_text="The name shown in the draw. Do not include institution name (see \"uses institutional prefix\" below)")
    institution = models.ForeignKey(Institution)
    tournament = models.ForeignKey(Tournament, db_index=True)
    emoji_seq = models.IntegerField(blank=True, null=True, help_text="Emoji number to use for this team")
    division = models.ForeignKey('Division', blank=True, null=True, on_delete=models.SET_NULL)
    use_institution_prefix = models.BooleanField(default=False, verbose_name="Uses institutional prefix", help_text="If ticked, a team called \"1\" from Victoria will be shown as \"Victoria 1\" ")
    url_key = models.SlugField(blank=True, null=True, unique=True, max_length=24)
    break_categories = models.ManyToManyField('breaking.BreakCategory', blank=True)

    venue_preferences = models.ManyToManyField('venues.VenueGroup',
        through = 'TeamVenuePreference',
        related_name = 'VenueGroup',
        verbose_name = 'Venue group preference'
    )

    TYPE_NONE = 'N'
    TYPE_SWING = 'S'
    TYPE_COMPOSITE = 'C'
    TYPE_BYE = 'B'
    TYPE_CHOICES = (
        (TYPE_NONE, 'None'),
        (TYPE_SWING, 'Swing'),
        (TYPE_COMPOSITE, 'Composite'),
        (TYPE_BYE, 'Bye'),
    )
    type = models.CharField(max_length=1, choices=TYPE_CHOICES,
                            default=TYPE_NONE)

    class Meta:
        unique_together = [('reference', 'institution', 'tournament'),('emoji_seq', 'tournament')]
        ordering = ['tournament', 'institution', 'short_reference']
        index_together = ['tournament', 'institution', 'short_reference']

    objects = TeamManager()

    def __unicode__(self):
        return u"%s - %s" % (self.tournament, self.short_name)

    @property
    def short_name(self):
        institution = self.get_cached_institution()
        if self.short_reference:
            name = self.short_reference
        else:
            name = self.reference
        if self.use_institution_prefix is True:
            if self.institution.code:
                return unicode(institution.code + " " + name)
            else:
                return unicode(institution.abbreviation + " " + name)
        else:
            return unicode(name)

    @property
    def long_name(self):
        institution = self.get_cached_institution()
        if self.use_institution_prefix is True:
            return unicode(institution.name + " " + self.reference)
        else:
            return unicode(self.reference)

    @property
    def region(self):
        return self.get_cached_institution().region

    @property
    def break_categories_nongeneral(self):
        return self.break_categories.exclude(is_general=True)

    @property
    def break_categories_str(self):
        categories = self.break_categories_nongeneral
        return "(" + ", ".join(c.name for c in categories) + ")" if categories else ""

    def get_aff_count(self, seq=None):
        return self._get_count(DebateTeam.POSITION_AFFIRMATIVE, seq)

    def get_neg_count(self, seq=None):
        return self._get_count(DebateTeam.POSITION_NEGATIVE, seq)


    def _get_count(self, position, seq):
        dts = self.debateteam_set.filter(position=position, debate__round__stage=Round.STAGE_PRELIMINARY)
        if seq is not None:
            dts = dts.filter(debate__round__seq__lte=seq)
        return dts.count()

    def get_debates(self, before_round):
        dts = self.debateteam_set.select_related('debate').order_by('debate__round__seq')
        if before_round is not None:
            dts = dts.filter(debate__round__seq__lt=before_round)
        return [dt.debate for dt in dts]

    @property
    def get_preferences(self):
        return self.teamvenuepreference_set.objects.all()

    @property
    def debates(self):
        return self.get_debates(None)

    @cached_property
    def wins_count(self):
        from results.models import TeamScore
        wins = TeamScore.objects.filter(ballot_submission__confirmed=True, debate_team__team=self, win=True).count()
        return wins

    @cached_property
    def speakers(self):
        return self.speaker_set.all().select_related('person_ptr')

    def seen(self, other, before_round=None):
        debates = self.get_debates(before_round)
        return len([1 for d in debates if other in d])

    def same_institution(self, other):
        return self.institution_id == other.institution_id

    def prev_debate(self, round_seq):
        try:
            return DebateTeam.objects.filter(
                debate__round__seq__lt=round_seq,
                team=self,
            ).order_by('-debate__round__seq')[0].debate
        except IndexError:
            return None

    def get_cached_institution(self):
        cached_key = "%s_%s_%s" % ('teamid', self.id, '_institution__object')
        cached_value = cache.get(cached_key)
        if cached_value:
            return cache.get(cached_key)
        else:
            cached_value = self.institution
            cache.set(cached_key, cached_value, None)
            return cached_value

def update_team_cache(sender, instance, created, **kwargs):
    cached_key = "%s_%s_%s" % ('teamid', instance.id, '_institution__object')
    cache.delete(cached_key)
    cached_key = "%s_%s_%s" % ('teamid', instance.id, '_speaker__objects')
    cache.delete(cached_key)

# Update the cached tournament object when model is changed)
signals.post_save.connect(update_team_cache, sender=Team)


class TeamVenuePreference(models.Model):
    team = models.ForeignKey(Team, db_index=True)
    venue_group = models.ForeignKey('venues.VenueGroup')
    priority = models.IntegerField()

    class Meta:
        ordering = ['priority',]

    def __unicode__(self):
        return u'%s with priority %s for %s' % (self.team, self.priority, self.venue_group)


class Person(models.Model):
    name = models.CharField(max_length=40, db_index=True)
    barcode_id = models.IntegerField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=40, blank=True, null=True)
    novice = models.BooleanField(default=False)

    checkin_message = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    GENDER_MALE = 'M'
    GENDER_FEMALE = 'F'
    GENDER_OTHER = 'O'
    GENDER_CHOICES = (
        (GENDER_MALE,     'Male'),
        (GENDER_FEMALE,   'Female'),
        (GENDER_OTHER,    'Other'),
    )
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True, null=True)
    pronoun = models.CharField(max_length=10, blank=True, null=True)

    @property
    def has_contact(self):
        return bool(self.email or self.phone)

    class Meta:
        ordering = ['name']



class Speaker(Person):
    team = models.ForeignKey(Team)

    def __unicode__(self):
        return unicode(self.name)


class AdjudicatorManager(models.Manager):
    use_for_related_fields = True

    def accredited(self):
        return self.filter(novice=False)

    def get_queryset(self):
        return super(AdjudicatorManager, self).get_queryset().select_related('institution')

class Adjudicator(Person):
    institution = models.ForeignKey(Institution)
    tournament = models.ForeignKey(Tournament, blank=True, null=True)
    test_score = models.FloatField(default=0)
    url_key = models.SlugField(blank=True, null=True, unique=True, max_length=24)

    institution_conflicts = models.ManyToManyField('Institution', through='AdjudicatorInstitutionConflict', related_name='adjudicator_institution_conflicts')
    conflicts = models.ManyToManyField('Team', through='AdjudicatorConflict')

    breaking = models.BooleanField(default=False)
    independent = models.BooleanField(default=False, blank=True)
    adj_core = models.BooleanField(default=False, blank=True)

    objects = AdjudicatorManager()

    class Meta:
        ordering = ['tournament', 'institution', 'name']

    def __unicode__(self):
        return u"%s (%s)" % (self.name, self.institution.code)

    def conflict_with(self, team):
        if not hasattr(self, '_conflict_cache'):
            self._conflict_cache = set(c['team_id'] for c in
                AdjudicatorConflict.objects.filter(adjudicator=self).values('team_id')
            )
            self._institution_conflict_cache = set(c['institution_id'] for c in
                AdjudicatorInstitutionConflict.objects.filter(adjudicator=self).values('institution_id')
            )
        return team.id in self._conflict_cache or team.institution_id in self._institution_conflict_cache

    @property
    def is_unaccredited(self):
        return self.novice

    @property
    def region(self):
        return self.institution.region

    @cached_property
    def score(self):
        if self.tournament:
            weight = self.tournament.current_round.feedback_weight
        else:
            # For shared ajudicators
            weight = 1

        feedback_score = self._feedback_score()
        if feedback_score is None:
            feedback_score = 0
            weight = 0

        return self.test_score * (1 - weight) + (weight * feedback_score)


    def _feedback_score(self):
        return self.adjudicatorfeedback_set.filter(confirmed=True).exclude(
                source_adjudicator__type=DebateAdjudicator.TYPE_TRAINEE).aggregate(
                avg=models.Avg('score'))['avg']

    @property
    def feedback_score(self):
        return self._feedback_score() or None

    def get_feedback(self):
        return self.adjudicatorfeedback_set.all()

    def seen_team(self, team, before_round=None):
        if not hasattr(self, '_seen_cache'):
            self._seen_cache = {}
        if before_round not in self._seen_cache:
            qs = DebateTeam.objects.filter(
                debate__debateadjudicator__adjudicator=self
            )
            if before_round is not None:
                qs = qs.filter(
                    debate__round__seq__lt = before_round.seq
                )
            self._seen_cache[before_round] = set(dt.team.id for dt in qs)
        return team.id in self._seen_cache[before_round]

    def seen_adjudicator(self, adj, before_round=None):
        d = DebateAdjudicator.objects.filter(
            adjudicator = self,
            debate__debateadjudicator__adjudicator = adj,
        )
        if before_round is not None:
            d = d.filter(
                debate__round__seq__lt = before_round.seq
            )
        return d.count()

class AdjudicatorConflict(models.Model):
    adjudicator = models.ForeignKey(Adjudicator)
    team = models.ForeignKey(Team)

class AdjudicatorAdjudicatorConflict(models.Model):
    adjudicator = models.ForeignKey(Adjudicator, related_name="source_adjudicator")
    conflict_adjudicator = models.ForeignKey(Adjudicator, related_name="target_adjudicator", verbose_name="Adjudicator")

class AdjudicatorInstitutionConflict(models.Model):
    adjudicator = models.ForeignKey(Adjudicator)
    institution = models.ForeignKey(Institution)


class RoundManager(models.Manager):
    use_for_related_Fields = True

    def lookup(self, name, **kwargs):
        """Queries for a round with matching name in any of the two name
        fields."""
        for field in ('name', 'abbreviation'):
            try:
                kwargs[field] = name
                return self.get(**kwargs)
            except ObjectDoesNotExist:
                kwargs.pop(field)
        raise self.model.DoesNotExist("No round matching '%s'" % name)

    def get_queryset(self):
        return super(RoundManager, self).get_queryset().select_related('tournament').order_by('seq')


class Round(models.Model):
    DRAW_RANDOM      = 'R'
    DRAW_MANUAL      = 'M'
    DRAW_ROUNDROBIN  = 'D'
    DRAW_POWERPAIRED = 'P'
    DRAW_FIRSTBREAK  = 'F'
    DRAW_BREAK       = 'B'
    DRAW_CHOICES = (
        (DRAW_RANDOM,      'Random'),
        (DRAW_MANUAL,      'Manual'),
        (DRAW_ROUNDROBIN,  'Round-robin'),
        (DRAW_POWERPAIRED, 'Power-paired'),
        (DRAW_FIRSTBREAK,  'First elimination'),
        (DRAW_BREAK,       'Subsequent elimination'),
    )

    STAGE_PRELIMINARY = 'P'
    STAGE_ELIMINATION = 'E'
    STAGE_CHOICES = (
        (STAGE_PRELIMINARY, 'Preliminary'),
        (STAGE_ELIMINATION, 'Elimination'),
    )

    STATUS_NONE      = 0
    STATUS_DRAFT     = 1
    STATUS_CONFIRMED = 10
    STATUS_RELEASED  = 99
    STATUS_CHOICES = (
        (STATUS_NONE,      'None'),
        (STATUS_DRAFT,     'Draft'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_RELEASED,  'Released'),
    )

    objects = RoundManager()

    tournament     = models.ForeignKey(Tournament, related_name='rounds', db_index=True)
    seq            = models.IntegerField(help_text="A number that determines the order of the round, IE 1 for the initial round")
    name           = models.CharField(max_length=40, help_text="e.g. \"Round 1\"")
    abbreviation   = models.CharField(max_length=10, help_text="e.g. \"R1\"")
    draw_type      = models.CharField(max_length=1, choices=DRAW_CHOICES, help_text="Which draw technique to use")
    stage          = models.CharField(max_length=1, choices=STAGE_CHOICES, default=STAGE_PRELIMINARY, help_text="Preliminary = inrounds, elimination = outrounds")
    break_category = models.ForeignKey('breaking.BreakCategory', blank=True, null=True, help_text="If elimination round, which break category")

    draw_status        = models.PositiveSmallIntegerField(choices=STATUS_CHOICES, default=STATUS_NONE)
    venue_status       = models.PositiveSmallIntegerField(choices=STATUS_CHOICES, default=STATUS_NONE)
    adjudicator_status = models.PositiveSmallIntegerField(choices=STATUS_CHOICES, default=STATUS_NONE)

    checkins = models.ManyToManyField('Person', through='availability.Checkin', related_name='checkedin_rounds')

    active_venues       = models.ManyToManyField('venues.Venue', through='availability.ActiveVenue')
    active_adjudicators = models.ManyToManyField('Adjudicator', through='availability.ActiveAdjudicator')
    active_teams        = models.ManyToManyField('Team', through='availability.ActiveTeam')

    feedback_weight = models.FloatField(default=0)
    silent = models.BooleanField(default=False)
    motions_released = models.BooleanField(default=False)
    starts_at = models.TimeField(blank=True, null=True)

    class Meta:
        unique_together = [('tournament', 'seq')]
        ordering = ['tournament', str('seq')]
        index_together = ['tournament', 'seq']

    def __unicode__(self):
        return u"%s - %s" % (self.tournament, self.name)

    def motions(self):
        return self.motion_set.order_by('seq')

    def draw(self, override_team_checkins=False):
        if self.draw_status != self.STATUS_NONE:
            raise RuntimeError("Tried to run draw on round that already has a draw")

        # Delete all existing debates for this round.
        Debate.objects.filter(round=self).delete()

        # There is a bit of logic to go through to figure out what we need to
        # provide to the draw class.
        OPTIONS_TO_CONFIG_MAPPING = {
            "avoid_institution"  : "avoid_same_institution",
            "avoid_history"      : "avoid_team_history",
            "history_penalty"    : "team_history_penalty",
            "institution_penalty": "team_institution_penalty",
            "side_allocations"   : "draw_side_allocations",
        }

        if override_team_checkins is True:
            draw_teams = Team.objects.filter(tournament=self.tournament).all()
        else:
            draw_teams = self.active_teams.all()

        # Set type-specific options
        if self.draw_type == self.DRAW_RANDOM:
            teams = draw_teams
            draw_type = "random"
            OPTIONS_TO_CONFIG_MAPPING.update({
                "avoid_conflicts" : "draw_avoid_conflicts",
            })
        elif self.draw_type == self.DRAW_MANUAL:
            teams = draw_teams
            draw_type = "manual"
        elif self.draw_type == self.DRAW_POWERPAIRED:
            teams = standings.annotate_team_standings(draw_teams, self.prev, shuffle=True)
            draw_type = "power_paired"
            OPTIONS_TO_CONFIG_MAPPING.update({
                "avoid_conflicts" : "draw_avoid_conflicts",
                "odd_bracket"     : "draw_odd_bracket",
                "pairing_method"  : "draw_pairing_method",
            })
        elif self.draw_type == self.DRAW_ROUNDROBIN:
            teams = draw_teams
            draw_type = "round_robin"
        else:
            raise RuntimeError("Break rounds aren't supported yet.")

        # Annotate attributes as required by DrawGenerator.
        if self.prev:
            for team in teams:
                team.aff_count = team.get_aff_count(self.prev.seq)
        else:
            for team in teams:
                team.aff_count = 0

        # Evaluate this query set first to avoid hitting the database inside a loop.
        tpas = dict()
        TPA_MAP = {TeamPositionAllocation.POSITION_AFFIRMATIVE: "aff",
            TeamPositionAllocation.POSITION_NEGATIVE: "neg"}
        for tpa in self.teampositionallocation_set.all():
            tpas[tpa.team] = TPA_MAP[tpa.position]
        for team in teams:
            if team in tpas:
                team.allocated_side = tpas[team]
        del tpas

        options = dict()
        for key, value in OPTIONS_TO_CONFIG_MAPPING.iteritems():
            options[key] = self.tournament.config.get(value)
        if options["side_allocations"] == "manual-ballot":
            options["side_allocations"] = "balance"

        drawer = DrawGenerator(draw_type, teams, results=None, **options)
        draw = drawer.make_draw()
        self.make_debates(draw)
        self.draw_status = self.STATUS_DRAFT
        self.save()

        #from debate.draw import assign_importance
        #assign_importance(self)

    def allocate_adjudicators(self, alloc_class=SAAllocator):
        if self.draw_status != self.STATUS_CONFIRMED:
            raise RuntimeError("Tried to allocate adjudicators on unconfirmed draw")

        debates = self.get_draw()
        adjs = list(self.active_adjudicators.accredited())
        allocator = alloc_class(debates, adjs)

        for alloc in allocator.allocate():
            alloc.save()
        self.adjudicator_status = self.STATUS_DRAFT
        self.save()

    @property
    def adjudicators_allocation_validity(self):
        debates = self.get_cached_draw
        if not all(debate.adjudicators.has_chair for debate in debates):
            return 1
        if not all(debate.adjudicators.valid for debate in debates):
            return 2
        return 0

    def venue_allocation_validity(self):
        debates = self.get_cached_draw
        if all(debate.venue for debate in debates):
            return True
        else:
            return False

    @cached_property
    def get_cached_draw(self):
        return self.get_draw()

    def get_draw(self):
        if self.tournament.config.get('enable_divisions'):
            debates = Debate.objects.filter(round=self).order_by('room_rank').select_related(
            'venue', 'division', 'division__venue_group')
        else:
            debates = Debate.objects.filter(round=self).order_by('room_rank').select_related(
            'venue')

        return debates

    def get_draw_by_room(self):
        if self.tournament.config.get('enable_divisions'):
            debates = Debate.objects.filter(round=self).order_by('venue__name').select_related(
                 'venue', 'division', 'division__venue_group')
        else:
            debates = Debate.objects.filter(round=self).order_by('venue__name').select_related(
                 'venue')

        return debates

    def get_draw_by_team(self):
        # TODO is there a more efficient way to do this?
        draw_by_team = list()
        for debate in self.debate_set.all():
            draw_by_team.append((debate.aff_team, debate))
            draw_by_team.append((debate.neg_team, debate))
        draw_by_team.sort(key=lambda x: str(x[0]))
        return draw_by_team

    def get_draw_with_standings(self, round):
        draw = self.get_draw()
        if round.prev:
            if round.tournament.config.get('team_points_rule') != "wadl":
                standings = list(Team.objects.subrank_standings(round.prev))
                for debate in draw:
                    for side in ('aff_team', 'neg_team'):
                        # TODO is there a more efficient way to do this?
                        team = getattr(debate, side)
                        setattr(debate, side + "_cached", team)
                        annotated_team = filter(lambda x: x == team, standings)
                        if len(annotated_team) == 1:
                            annotated_team = annotated_team[0]
                            team.points = annotated_team.points
                            team.speaker_score = annotated_team.speaker_score
                            team.subrank = annotated_team.subrank
                            team.draw_strength = getattr(annotated_team, 'draw_strength', None) # only exists in NZ standings rules
                            if annotated_team.points:
                                team.pullup = abs(annotated_team.points - debate.bracket) >= 1 # don't highlight intermediate brackets that look within reason
            else:
                standings = list(Team.objects.standings(round.prev))

        return draw

    def make_debates(self, pairings):

        import random
        venues = list(self.active_venues.order_by('-priority'))[:len(pairings)]

        if len(venues) < len(pairings):
            raise DrawError("There are %d debates but only %d venues." % (len(pairings), len(venues)))

        random.shuffle(venues)
        random.shuffle(pairings) # to avoid IDs indicating room ranks

        for pairing in pairings:
            try:
                if pairing.division:
                    if (pairing.teams[0].type == "B") or (pairing.teams[1].type == "B"):
                        # If the match is a bye then they don't get a venue
                        selected_venue = None
                    else:
                        selected_venue = next(v for v in venues if v.group == pairing.division.venue_group)
                        venues.pop(venues.index(selected_venue))
                else:
                    selected_venue = venues.pop(0)
            except:
                print "Error assigning venues"
                selected_venue = None

            debate = Debate(round=self, venue=selected_venue)

            debate.division = pairing.division
            debate.bracket   = pairing.bracket
            debate.room_rank = pairing.room_rank
            debate.flags     = ",".join(pairing.flags) # comma-separated list
            debate.save()

            aff = DebateTeam(debate=debate, team=pairing.teams[0], position=DebateTeam.POSITION_AFFIRMATIVE)
            neg = DebateTeam(debate=debate, team=pairing.teams[1], position=DebateTeam.POSITION_NEGATIVE)

            aff.save()
            neg.save()

    # TODO: these availability methods should probably be rolled into the app

    def base_availability(self, model, active_table, active_column, model_table,
                         id_field='id'):
        d = {
            'active_table' : active_table,
            'active_column' : active_column,
            'model_table': model_table,
            'id_field': id_field,
            'id' : self.id,
        }
        return model.objects.all().extra(select={'is_active': """EXISTS (Select 1
                                                 from %(active_table)s
                                                 drav where
                                                 drav.%(active_column)s =
                                                 %(model_table)s.%(id_field)s and
                                                 drav.round_id=%(id)d)""" % d })

    def person_availability(self):
        return self.base_availability(Person, 'availability_checkin', 'person_id',
                                      'debate_person')


    def venue_availability(self):
        from venues.models import Venue
        all_venues = self.base_availability(Venue, 'availability_activevenue', 'venue_id',
                                      'venues_venue')
        all_venues = [v for v in all_venues if v.tournament == self.tournament]
        return all_venues

    def unused_venues(self):
        from venues.models import Venue
        # Had to replicate venue_availability via base_availability so extra()
        # could still function on the query set
        result = self.base_availability(Venue, 'availability_activevenue', 'venue_id',
                                      'venues_venue').extra(select =
                                      {'is_used': """EXISTS (SELECT 1
                                      FROM debate_debate da
                                      WHERE da.round_id=%d AND
                                      da.venue_id = venues_venue.id)""" % self.id},
        )
        return [v for v in result if v.is_active and not v.is_used and v.tournament == self.tournament]

    def adjudicator_availability(self):
        all_adjs = self.base_availability(Adjudicator, 'availability_activeadjudicator',
                                      'adjudicator_id',
                                      'debate_adjudicator', id_field='person_ptr_id')

        if not self.tournament.config.get('share_adjs'):
            all_adjs = [a for a in all_adjs if a.tournament == self.tournament]

        return all_adjs

    def unused_adjudicators(self):
        result = self.base_availability(Adjudicator, 'availability_activeadjudicator',
                                      'adjudicator_id',
                                      'debate_adjudicator',
                                      id_field='person_ptr_id').extra(
                                        select = {'is_used': """EXISTS (SELECT 1
                                                  FROM debate_debateadjudicator da
                                                  LEFT JOIN debate_debate d ON da.debate_id = d.id
                                                  WHERE d.round_id = %d AND
                                                  da.adjudicator_id = debate_adjudicator.person_ptr_id)""" % self.id },
        )
        if not self.tournament.config.get('draw_skip_adj_checkins'):
            return [a for a in result if a.is_active and not a.is_used]
        else:
            return [a for a in result if not a.is_used]

    def team_availability(self):
        all_teams = self.base_availability(Team, 'availability_activeteam', 'team_id',
                                      'debate_team')
        relevant_teams = [t for t in all_teams if t.tournament == self.tournament]
        return relevant_teams

    def unused_teams(self):
        all_teams = self.active_teams.all()
        all_teams = [t for t in all_teams if t.tournament == self.tournament]

        debating_teams = [t.team for t in DebateTeam.objects.filter(debate__round=self).select_related('team', 'debate')]
        unused_teams = [t for t in all_teams if t not in debating_teams]

        return unused_teams

    def set_available_base(self, ids, model, active_model, get_active,
                             id_column, active_id_column, remove=True):
        ids = set(ids)
        all_ids = set(a['id'] for a in model.objects.values('id'))
        exclude_ids = all_ids.difference(ids)
        existing_ids = set(a['id'] for a in get_active.values('id'))

        remove_ids = existing_ids.intersection(exclude_ids)
        add_ids = ids.difference(existing_ids)

        if remove:
            active_model.objects.filter(**{
                '%s__in' % active_id_column: remove_ids,
                'round': self,
            }).delete()

        for id in add_ids:
            m = active_model(round=self)
            setattr(m, id_column, id)
            m.save()

    def set_available_people(self, ids):
        from availability import Checkin
        return self.set_available_base(ids, Person, Checkin,
                                      self.checkins, 'person_id',
                                      'person__id', remove=False)

    def set_available_venues(self, ids):
        from availability.models import ActiveVenue
        from venues.models import Venue
        return self.set_available_base(ids, Venue, ActiveVenue,
                                       self.active_venues, 'venue_id',
                                       'venue__id')

    def set_available_adjudicators(self, ids):
        from availability.models import ActiveAdjudicator
        return self.set_available_base(ids, Adjudicator, ActiveAdjudicator,
                                       self.active_adjudicators,
                                       'adjudicator_id', 'adjudicator__id')

    def set_available_teams(self, ids):
        from availability.models import ActiveTeam
        return self.set_available_base(ids, Team, ActiveTeam,
                                       self.active_teams, 'team_id',
                                      'team__id')

    def activate_adjudicator(self, adj, state=True):
        from availability.models import ActiveAdjudicator
        if state:
            ActiveAdjudicator.objects.get_or_create(round=self, adjudicator=adj)
        else:
            ActiveAdjudicator.objects.filter(round=self,
                                             adjudicator=adj).delete()

    def activate_venue(self, venue, state=True):
        from availability.models import ActiveVenue
        if state:
            ActiveVenue.objects.get_or_create(round=self, venue=venue)
        else:
            ActiveVenue.objects.filter(round=self, venue=venue).delete()

    def activate_team(self, team, state=True):
        from availability.models import ActiveTeam
        if state:
            ActiveTeam.objects.get_or_create(round=self, team=team)
        else:
            ActiveTeam.objects.filter(round=self, team=team).delete()

    def activate_all(self):
        from venues.models import Venue
        self.set_available_venues([v.id for v in Venue.objects.all()])
        self.set_available_adjudicators([a.id for a in
                                         Adjudicator.objects.all()])
        self.set_available_teams([t.id for t in Team.objects.all()])

    @property
    def prev(self):
        try:
            return Round.objects.get(seq=self.seq-1, tournament=self.tournament)
        except Round.DoesNotExist:
            return None

    @property
    def motions_good_for_public(self):
        return self.motions_released or not self.motion_set.exists()

def update_round_cache(sender, instance, created, **kwargs):
    cached_key = "%s_%s_%s" % (instance.tournament.slug, instance.seq, 'object')
    cache.delete(cached_key)
    logger.info("Updated cache %s for %s" % (cached_key, instance))

# Update the cached round object when model is changed)
signals.post_save.connect(update_round_cache, sender=Round)




class DebateManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super(DebateManager, self).get_queryset().select_related(
            'round')

class Debate(models.Model):
    STATUS_NONE      = 'N'
    STATUS_POSTPONED = 'P'
    STATUS_DRAFT     = 'D'
    STATUS_CONFIRMED = 'C'
    STATUS_CHOICES = (
        (STATUS_NONE,      'None'),
        (STATUS_POSTPONED, 'Postponed'),
        (STATUS_DRAFT,     'Draft'),
        (STATUS_CONFIRMED, 'Confirmed'),
    )

    objects = DebateManager()

    round = models.ForeignKey(Round, db_index=True)
    venue = models.ForeignKey('venues.Venue', blank=True, null=True)
    division = models.ForeignKey('Division', blank=True, null=True)

    bracket = models.FloatField(default=0)
    room_rank = models.IntegerField(default=0)

    # comma-separated list of strings
    flags = models.CharField(max_length=100, blank=True, null=True)

    importance = models.IntegerField(default=2)
    result_status = models.CharField(max_length=1, choices=STATUS_CHOICES,
            default=STATUS_NONE)
    ballot_in = models.BooleanField(default=False)

    def __contains__(self, team):
        return team in (self.aff_team, self.neg_team)

    def __unicode__(self):
        try:
            return u"%s - [%s] %s vs %s" % (
                self.round.tournament,
                self.round.abbreviation,
                self.aff_team.short_name,
                self.neg_team.short_name
            )
        except DebateTeam.DoesNotExist:
            return u"%s - [%s] %s" % (
                self.round.tournament,
                self.round.abbreviation,
                ", ".join(map(lambda x: x.short_name, self.teams))
            )

    @property
    def teams(self):
        return Team.objects.select_related('debate_team').filter(debateteam__debate=self)

    @cached_property
    def aff_team(self):
        aff_dt = self.aff_dt
        return aff_dt.team

    @cached_property
    def neg_team(self):
        neg_dt = self.neg_dt
        return neg_dt.team

    def get_team(self, side):
        return getattr(self, '%s_team' % side)

    def get_dt(self, side):
        """dt = DebateTeam"""
        return getattr(self, '%s_dt' % side)

    @cached_property
    def aff_dt(self):
        aff_dt = DebateTeam.objects.select_related('team', 'team__institution').get(debate=self, position=DebateTeam.POSITION_AFFIRMATIVE)
        return aff_dt

    @cached_property
    def neg_dt(self):
        neg_dt = DebateTeam.objects.select_related('team', 'team__institution').get(debate=self, position=DebateTeam.POSITION_NEGATIVE)
        return neg_dt

    def get_side(self, team):
        if self.aff_team == team:
            return 'aff'
        if self.neg_team == team:
            return 'neg'
        return None

    @cached_property
    def draw_conflicts(self):
        d = []
        history = self.aff_team.seen(self.neg_team, before_round=self.round.seq)
        if history:
            d.append("History conflict (%d)" % history)
        if self.aff_team.institution == self.neg_team.institution:
            d.append("Institution conflict")

        return d

    @cached_property
    def confirmed_ballot(self):
        """Returns the confirmed BallotSubmission for this debate, or None if
        there is no such ballot submission."""
        try:
            return self.ballotsubmission_set.get(confirmed=True)
        except ObjectDoesNotExist: # BallotSubmission isn't defined yet, so can't use BallotSubmission.DoesNotExist
            return None

    @property
    def ballotsubmission_set_by_version(self):
        return self.ballotsubmission_set.order_by('version')

    @property
    def ballotsubmission_set_by_version_except_discarded(self):
        return self.ballotsubmission_set.filter(discarded=False).order_by('version')

    @property
    def identical_ballotsubs_dict(self):
        """Returns a dict. Keys are BallotSubmissions, values are lists of
        version numbers of BallotSubmissions that are identical to the key's
        BallotSubmission. Excludes discarded ballots (always)."""
        ballotsubs = self.ballotsubmission_set_by_version_except_discarded
        result = {b: list() for b in ballotsubs}
        for ballotsub1 in ballotsubs:
            # Save a bit of time by avoiding comparisons already done.
            # This relies on ballots being ordered by version.
            for ballotsub2 in ballotsubs.filter(version__gt=ballotsub1.version):
                if ballotsub1.is_identical(ballotsub2):
                    result[ballotsub1].append(ballotsub2.version)
                    result[ballotsub2].append(ballotsub1.version)
        for l in result.itervalues():
            l.sort()
        return result

    @property
    def flags_all(self):
        if not self.flags:
            return []
        else:
            return [DRAW_FLAG_DESCRIPTIONS[f] for f in self.flags.split(",")]

    @property
    def all_conflicts(self):
        return self.draw_conflicts + self.adjudicator_conflicts

    @cached_property
    def adjudicator_conflicts(self):
        class Conflict(object):
            def __init__(self, adj, team):
                self.adj = adj
                self.team = team
            def __unicode__(self):
                return u'Adj %s + %s' % (self.adj, self.team)

        a = []
        for t, adj in self.adjudicators:
            for team in (self.aff_team, self.neg_team):
                if adj.conflict_with(team):
                    a.append(Conflict(adj, team))

        return a

    @cached_property
    def adjudicators(self):
        """Returns an AdjudicatorAllocation containing the adjudicators for this
        debate."""
        adjs = DebateAdjudicator.objects.filter(debate=self).select_related('adjudicator')
        alloc = AdjudicatorAllocation(self)
        for a in adjs:
            if a.type == a.TYPE_CHAIR:
                alloc.chair = a.adjudicator
            if a.type == a.TYPE_PANEL:
                alloc.panel.append(a.adjudicator)
            if a.type == a.TYPE_TRAINEE:
                alloc.trainees.append(a.adjudicator)
        return alloc

    @property
    def chair(self):
        da_adj = list(DebateAdjudicator.objects.filter(debate=self, type="C"))
        a_adj = da_adj[0].adjudicator
        return a_adj

    @property
    def matchup(self):
        return u'%s vs %s' % (self.aff_team.short_name, self.neg_team.short_name)

    @property
    def division_motion(self):
        return Motion.objects.filter(round=self.round, divisions=self.division)


class SRManager(models.Manager):
    use_for_related_fields = True
    def get_queryset(self):
        return super(SRManager, self).get_queryset().select_related('debate')


class DebateTeam(models.Model):
    POSITION_AFFIRMATIVE = 'A'
    POSITION_NEGATIVE = 'N'
    POSITION_UNALLOCATED = 'u'
    POSITION_CHOICES = (
        (POSITION_AFFIRMATIVE, 'Affirmative'),
        (POSITION_NEGATIVE, 'Negative'),
        (POSITION_UNALLOCATED, 'Unallocated'),
    )

    objects = SRManager()

    debate = models.ForeignKey(Debate, db_index=True)
    team = models.ForeignKey(Team)
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    def __unicode__(self):
        return u'%s (%s)' % (self.team, self.debate)

    @cached_property # TODO: this slows down the standings pages reasonably heavily
    def opposition(self):
        try:
            return DebateTeam.objects.exclude(position=self.position).get(debate=self.debate)
        except (DebateTeam.DoesNotExist, DebateTeam.MultipleObjectsReturned):
            logger.error("Error finding opposition: %s, %s", self.debate, self.position)
            return None

    @cached_property
    def result(self):
        """Returns 'won' if won, 'lost' if lost, 'result unknown' if no result confirmed."""
        if self.debate.confirmed_ballot and self.debate.confirmed_ballot.ballot_set:
            ballotset = self.debate.confirmed_ballot.ballot_set
            if ballotset.aff_win and self.position == DebateTeam.POSITION_AFFIRMATIVE:
                return 'won'
            if ballotset.neg_win and self.position == DebateTeam.POSITION_NEGATIVE:
                return 'won'
            return 'lost'
        return 'result unknown'


class DebateAdjudicator(models.Model):
    TYPE_CHAIR = 'C'
    TYPE_PANEL = 'P'
    TYPE_TRAINEE = 'T'

    TYPE_CHOICES = (
        (TYPE_CHAIR,   'chair'),
        (TYPE_PANEL,   'panellist'),
        (TYPE_TRAINEE, 'trainee'),
    )

    objects = SRManager()

    debate = models.ForeignKey(Debate, db_index=True)
    adjudicator = models.ForeignKey(Adjudicator, db_index=True)
    type = models.CharField(max_length=2, choices=TYPE_CHOICES)

    def __unicode__(self):
        return u'%s %s' % (self.adjudicator, self.debate)


class TeamPositionAllocation(models.Model):
    """Model to store team position allocations for tournaments like Joynt
    Scroll (New Zealand). Each team-round combination should have one of these.
    In tournaments without team position allocations, just don't use this
    model."""

    POSITION_AFFIRMATIVE = DebateTeam.POSITION_AFFIRMATIVE
    POSITION_NEGATIVE = DebateTeam.POSITION_NEGATIVE
    POSITION_UNALLOCATED = DebateTeam.POSITION_UNALLOCATED
    POSITION_CHOICES = DebateTeam.POSITION_CHOICES

    round = models.ForeignKey(Round)
    team = models.ForeignKey(Team)
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    class Meta:
        unique_together = [('round', 'team')]


class Submission(models.Model):
    """Abstract base class to provide functionality common to different
    types of submissions.

    The unique_together class attribute of the Meta class MUST be set in
    all subclasses."""

    SUBMITTER_TABROOM = 0
    SUBMITTER_PUBLIC  = 1
    SUBMITTER_TYPE_CHOICES = (
        (SUBMITTER_TABROOM, 'Tab room'),
        (SUBMITTER_PUBLIC,  'Public'),
    )

    timestamp = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField()
    submitter_type = models.PositiveSmallIntegerField(choices=SUBMITTER_TYPE_CHOICES)

    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True, null=True, related_name="%(app_label)s_%(class)s_submitted") # only relevant if submitter was in tab room
    confirmer = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True, null=True, related_name="%(app_label)s_%(class)s_confirmed")
    confirm_timestamp = models.DateTimeField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    version_semaphore = BoundedSemaphore()

    confirmed = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True

    @property
    def _unique_filter_args(self):
        return dict((arg, getattr(self, arg)) for arg in self._meta.unique_together[0] if arg != 'version')

    def save(self, *args, **kwargs):
        # Check for uniqueness.
        if self.confirmed:
            try:
                current = self.__class__.objects.get(confirmed=True, **self._unique_filter_args)
            except self.DoesNotExist:
                pass
            else:
                if current != self:
                    warn("%s confirmed while %s was already confirmed, setting latter to unconfirmed" % (self, current))
                    current.confirmed = False
                    current.save()

        # Assign the version field to one more than the current maximum version.
        # Use a semaphore to protect against the possibility that two submissions do this
        # at the same time and get the same version number.
        self.version_semaphore.acquire()
        if self.pk is None:
            existing = self.__class__.objects.filter(**self._unique_filter_args)
            if existing.exists():
                self.version = existing.aggregate(models.Max('version'))['version__max'] + 1
            else:
                self.version = 1
        super(Submission, self).save(*args, **kwargs)
        self.version_semaphore.release()

    def clean(self):
        if self.submitter_type == self.SUBMITTER_TABROOM and self.submitter is None:
            raise ValidationError("A tab room ballot must have a user associated.")



class AdjudicatorAllocation(object):
    """Not a model, just a container object for the adjudicators on a panel."""
    def __init__(self, debate, chair=None, panel=None):
        self.debate = debate
        self.chair = chair
        self.panel = panel or []
        self.trainees = []

    @property
    def list(self):
        """Panel only, excludes trainees."""
        a = [self.chair]
        a.extend(self.panel)
        return a

    def __unicode__(self):
        return ", ".join(map(lambda x: (x is not None) and x.name or "<None>", self.list))

    def __iter__(self):
        """Iterates through all, including trainees."""
        if self.chair is not None:
            yield DebateAdjudicator.TYPE_CHAIR, self.chair
        for a in self.panel:
            yield DebateAdjudicator.TYPE_PANEL, a
        for a in self.trainees:
            yield DebateAdjudicator.TYPE_TRAINEE, a

    def __contains__(self, item):
        return item == self.chair or item in self.panel or item in self.trainees

    def delete(self):
        """Delete existing, current allocation"""
        self.debate.debateadjudicator_set.all().delete()
        self.chair = None
        self.panel = []
        self.trainees = []

    @property
    def has_chair(self):
        return self.chair is not None

    @property
    def is_panel(self):
        return len(self.panel) > 0

    @property
    def valid(self):
        return self.has_chair and len(self.panel) % 2 == 0

    def save(self):
        self.debate.debateadjudicator_set.all().delete()
        for t, adj in self:
            if isinstance(adj, Adjudicator):
                adj = adj.id
            if adj:
                DebateAdjudicator(debate=self.debate, adjudicator_id=adj, type=t).save()

