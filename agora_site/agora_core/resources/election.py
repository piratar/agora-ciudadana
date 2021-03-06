from agora_site.agora_core.models import Election, CastVote
from agora_site.agora_core.tasks.election import (start_election, end_election,
    archive_election)
from agora_site.misc.generic_resource import GenericResource, GenericMeta
from agora_site.agora_core.resources.user import UserResource
from agora_site.agora_core.resources.agora import AgoraResource, TinyAgoraResource
from agora_site.agora_core.resources.castvote import CastVoteResource
from agora_site.agora_core.forms import PostCommentForm, election_questions_validator
from agora_site.agora_core.forms.election import VoteForm as ElectionVoteForm
from agora_site.misc.utils import (geolocate_ip, get_base_email_context,
    JSONFormField)
from agora_site.misc.decorators import permission_required

from tastypie import fields, http
from tastypie.authorization import Authorization
from tastypie.exceptions import ImmediateHttpResponse
from tastypie.validation import Validation, CleanedDataFormValidation
from tastypie.utils import trailing_slash

from actstream.signals import action
from actstream.models import object_stream

from django.conf.urls.defaults import url
from django.contrib.sites.models import Site
from django.core.mail import EmailMultiAlternatives, EmailMessage, send_mass_mail
from django.template.loader import render_to_string
from django.forms import ModelForm
from django.utils.translation import ugettext as _
from django.utils import simplejson as json
from django.utils import translation
from django.db import transaction
from django import forms as django_forms

import datetime



DELEGATION_URL = "http://example.com/delegation/has/no/url/"
CAST_VOTE_RESOURCE = 'agora_site.agora_core.resources.castvote.CastVoteResource'


class TinyElectionResource(GenericResource):
    '''
    Tiny Resource representing elections.

    Typically used to include the critical election information in other
    resources, as in ActionResource for example.
    '''

    content_type = fields.CharField(default="election")
    agora = fields.ForeignKey(TinyAgoraResource, 'agora', full=True)
    url = fields.CharField()
    mugshot_url = fields.CharField()

    class Meta(GenericMeta):
        queryset = Election.objects.all()
        fields = ['name', 'pretty_name', 'id', 'short_description']

    def dehydrate_url(self, bundle):
        return bundle.obj.get_link()

    def dehydrate_mugshot_url(self, bundle):
        return bundle.obj.get_mugshot_url()


class ElectionAdminForm(ModelForm):
    '''
    Form used to validate election administration details.
    '''
    questions = JSONFormField(label=_('Questions'), required=True,
        validators=[election_questions_validator])
    from_date = django_forms.DateTimeField(label=_('Start voting'), required=False,
        input_formats=('%Y-%m-%dT%H:%M:%S',))
    to_date = django_forms.DateTimeField(label=_('End voting'), required=False,
        input_formats=('%Y-%m-%dT%H:%M:%S',))

    class Meta(GenericMeta):
        model = Election
        fields = ('pretty_name', 'short_description', 'description',
            'is_vote_secret', 'comments_policy')

    def __init__(self, **kwargs):
        self.request = kwargs.get('request', None)
        del kwargs['request']
        super(ElectionAdminForm, self).__init__(**kwargs)

    def clean(self, *args, **kwargs):
        cleaned_data = super(ElectionAdminForm, self).clean()
        if not self.instance.has_perms('edit_details', self.request.user):
            raise ImmediateHttpResponse(response=http.HttpForbidden())

        from_date = cleaned_data.get("from_date", None)
        to_date = cleaned_data.get("to_date", None)

        if not from_date and not to_date:
            return cleaned_data

        if from_date < datetime.datetime.now():
            raise django_forms.ValidationError(_('Invalid start date, must be '
                'in the future'))

        if from_date and to_date and ((to_date - from_date) < datetime.timedelta(hours=1)):
            raise django_forms.ValidationError(_('Voting time must be at least 1 hour'))

        return cleaned_data

    def save(self, *args, **kwargs):
        election = super(ElectionAdminForm, self).save(*args, **kwargs)

        # Questions/answers have a special formatting
        election.questions = self.cleaned_data['questions']
        election.last_modified_at_date = datetime.datetime.now()
        election.save()

        kwargs=dict(
            election_id=election.id,
            is_secure=self.request.is_secure(),
            site_id=Site.objects.get_current().id,
            remote_addr=self.request.META.get('REMOTE_ADDR'),
            user_id=self.request.user.id
        )

        if ("from_date" in self.cleaned_data):
            from_date = self.cleaned_data["from_date"]
            election.voting_starts_at_date = from_date
            election.save()
            transaction.commit()

            start_election.apply_async(kwargs=kwargs, task_id=election.task_id(start_election),
                eta=election.voting_starts_at_date)

        if "to_date" in self.cleaned_data:
            to_date = self.cleaned_data["to_date"]
            election.voting_extended_until_date = election.voting_ends_at_date = to_date
            election.save()
            transaction.commit()
            end_election.apply_async(kwargs=kwargs, task_id=election.task_id(end_election),
                eta=election.voting_ends_at_date)

        return election

class ElectionFormValidation(CleanedDataFormValidation):
     def is_valid(self, bundle, request):
        """
        Performs a check on ``bundle.data``to ensure it is valid.

        If the form is valid, an empty list (all valid) will be returned. If
        not, a list of errors will be returned.
        """
        kwargs = self.form_args(bundle)
        kwargs['request'] = request

        form = self.form_class(**kwargs)

        if form.is_valid() and form.save():
            return {}

        # The data is invalid. Let's collect all the error messages & return
        # them.
        return form.errors

class ElectionValidation(Validation):
    '''
    Validation class that uses some django forms to validate PUT and POST
    methods.
    '''
    def is_valid(self, bundle, request):
        if not bundle.data:
            return {'__all__': 'Not quite what I had in mind.'}

        elif request.method == "PUT":
            return self.validate_put(bundle, request)

        return {}

    def validate_put(self, bundle, request):
        if not bundle.obj.has_perms('edit_details', request.user):
            raise ImmediateHttpResponse(response=http.HttpForbidden())

        form = ElectionFormValidation(form_class=ElectionAdminForm)
        return form.is_valid(bundle, request)


class ElectionResource(GenericResource):
    '''
    Resource representing elections.
    '''

    creator = fields.ForeignKey(UserResource, 'creator')

    # TODO: electorate should be a separate call like members in an agora.
    # besides, it's not being used yet
    #electorate = fields.ManyToManyField(UserResource, 'electorate')

    agora = fields.ForeignKey(TinyAgoraResource, 'agora', full=True)

    parent_election = fields.ForeignKey('self', 'parent_election', null=True)

    percentage_of_participation = fields.IntegerField()

    direct_votes_count = fields.IntegerField()

    # this will be only indicative when election has not been tallied
    delegated_votes_count = fields.IntegerField()

    url = fields.CharField()

    mugshot_url = fields.CharField()

    class Meta(GenericMeta):
        queryset = Election.objects\
                    .exclude(url__startswith=DELEGATION_URL)\
                    .order_by('-last_modified_at_date')
        #authentication = SessionAuthentication()
        authorization = Authorization()
        validation = ElectionValidation()
        always_return_data = True
        list_allowed_methods = ['get', 'post']
        detail_allowed_methods = ['get', 'put']

        excludes = ['PROHIBITED_ELECTION_NAMES']

    def dehydrate_url(self, bundle):
        return bundle.obj.get_link()

    def dehydrate_mugshot_url(self, bundle):
        return bundle.obj.get_mugshot_url()

    def dehydrate_direct_votes_count(self, bundle):
        return bundle.obj.get_direct_votes().count()

    def dehydrate_delegated_votes_count(self, bundle):
        return bundle.obj.get_delegated_votes().count()

    def prepend_urls(self):
        return [
            # all counting votes
            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/all_votes%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_all_votes'), name="api_election_all_votes"),

            # all votes, valid and invalid, counting or not
            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/cast_votes%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_cast_votes'), name="api_election_cast_votes"),

            # all indirect votes that are valid - only available when election is tallied
            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/delegated_votes%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_delegated_votes'), name="api_election_delegated_votes"),

            # all countable direct votes that are public
            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/votes_from_delegates%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_votes_from_delegates'), name="api_election_votes_from_delegates"),

            # all countable direct votes
            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/direct_votes%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_direct_votes'), name="api_election_direct_votes"),

            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/comments%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('get_comments'), name="api_election_comments"),

            url(r"^(?P<resource_name>%s)/(?P<election>\d+)/add_comment%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('add_comment'), name="api_election_add_comment"),

            url(r"^(?P<resource_name>%s)/(?P<electionid>\d+)/action%s$" \
                % (self._meta.resource_name, trailing_slash()),
                self.wrap_view('action'), name="api_election_action"),
        ]

    def get_comments(self, request, **kwargs):
        '''
        List the comments in this election
        '''
        from actstream.resources import ActionResource
        return self.get_custom_resource_list(request, resource=ActionResource,
            queryfunc=lambda election: object_stream(election, verb='commented'), **kwargs)

    @permission_required('comment', (Election, 'id', 'election'))
    def add_comment(self, request, **kwargs):
        '''
        Form to add comments
        '''
        return self.wrap_form(PostCommentForm)(request, **kwargs)

    def action(self, request, **kwargs):
        '''
        Requests an action on this election

        actions:
            DONE
            * get_permissions
            * approve
            * start
            * stop
            * archive
            * vote
            * cancel_vote
        '''

        actions = {
            'get_permissions': self.get_permissions_action,
            'approve': self.approve_action,
            'start': self.start_action,
            'stop': self.stop_action,
            'archive': self.archive_action,
            'vote': self.vote_action,
            'cancel_vote': self.cancel_vote_action,
        }

        if request.method != "POST":
            raise ImmediateHttpResponse(response=http.HttpMethodNotAllowed())

        data = self.deserialize_post_data(request)

        election = None
        electionid = kwargs.get('electionid', -1)
        try:
            election = Election.objects.get(id=electionid)
        except:
            raise ImmediateHttpResponse(response=http.HttpNotFound())

        action = data.get("action", False)

        if not action or not action in actions:
            raise ImmediateHttpResponse(response=http.HttpNotFound())

        kwargs.update(data)
        return actions[action](request, election, **kwargs)


    def get_permissions_action(self, request, election, **kwargs):
        '''
        Returns the permissions the user that requested it has
        '''
        return self.create_response(request,
            dict(permissions=election.get_perms(request.user)))


    @permission_required('approve_election', (Election, 'id', 'electionid'))
    def approve_action(self, request, election, **kwargs):
        '''
        Requests membership from authenticated user to an agora
        '''
        election.is_approved = True
        election.save()

        action.send(request.user, verb='approved', action_object=election,
            target=election.agora, ipaddr=request.META.get('REMOTE_ADDR'),
            geolocation=json.dumps(geolocate_ip(request.META.get('REMOTE_ADDR'))))

        # Mail to the user
        if request.user.get_profile().has_perms('receive_email_updates'):
            translation.activate(request.user.get_profile().lang_code)
            context = get_base_email_context(request)
            context.update(dict(
                agora=election.agora,
                other_user=request.user,
                notification_text=_('Election %(election)s approved at agora '
                    '%(agora)s. Congratulations!') % dict(
                        agora=election.agora.get_full_name(),
                        election=election.pretty_name
                    ),
                to=request.user,
                extra_urls=[dict(
                    url_title=_("Election URL"),
                    url=election.get_link()
                )]
            ))

            email = EmailMultiAlternatives(
                subject=_('%(site)s - Election %(election)s approved') % dict(
                            site=Site.objects.get_current().domain,
                            election=election.pretty_name
                        ),
                body=render_to_string('agora_core/emails/agora_notification.txt',
                    context),
                to=[request.user.email])

            email.attach_alternative(
                render_to_string('agora_core/emails/agora_notification.html',
                    context), "text/html")
            email.send()
            translation.deactivate()

        return self.create_response(request, dict(status="success"))

    @permission_required('begin_election', (Election, 'id', 'electionid'))
    def start_action(self, request, election, **kwargs):
        '''
        Starts an election
        '''

        tkwargs=dict(
            election_id=election.id,
            is_secure=request.is_secure(),
            site_id=Site.objects.get_current().id,
            remote_addr=request.META.get('REMOTE_ADDR'),
            user_id=request.user.id
        )

        election.last_modified_at_date = datetime.datetime.now()
        election.voting_starts_at_date = election.last_modified_at_date
        if not election.frozen_at_date:
            election.frozen_at_date = election.last_modified_at_date
        election.save()
        transaction.commit()

        start_election.apply_async(kwargs=tkwargs, task_id=election.task_id(start_election))
        return self.create_response(request, dict(status="success"))

    @permission_required('end_election', (Election, 'id', 'electionid'))
    def stop_action(self, request, election, **kwargs):
        '''
        Ends an election
        '''
        election.last_modified_at_date = datetime.datetime.now()

        tkwargs=dict(
            election_id=election.id,
            is_secure=request.is_secure(),
            site_id=Site.objects.get_current().id,
            remote_addr=request.META.get('REMOTE_ADDR'),
            user_id=request.user.id
        )

        election.voting_ends_at_date = datetime.datetime.now()
        election.voting_extended_until_date = election.voting_ends_at_date
        election.save()
        transaction.commit()

        end_election.apply_async(kwargs=tkwargs, task_id=election.task_id(end_election))
        return self.create_response(request, dict(status="success"))

    @permission_required('archive_election', (Election, 'id', 'electionid'))
    def archive_action(self, request, election, **kwargs):
        '''
        Archives an election
        '''
        election.archived_at_date = datetime.datetime.now()
        election.last_modified_at_date = election.archived_at_date

        if election.has_started() and not election.has_ended():
            election.voting_ends_at_date = election.archived_at_date
            election.voting_extended_until_date = election.archived_at_date

        if not election.is_frozen():
            election.frozen_at_date = election.archived_at_date

        election.save()
        transaction.commit()

        tkwargs=dict(
            election_id=election.id,
            is_secure=request.is_secure(),
            site_id=Site.objects.get_current().id,
            remote_addr=request.META.get('REMOTE_ADDR'),
            user_id=request.user.id
        )

        archive_election.apply_async(kwargs=tkwargs, task_id=election.task_id(archive_election))
        return self.create_response(request, dict(status="success"))

    @permission_required('emit_direct_vote', (Election, 'id', 'electionid'))
    def vote_action(self, request, election, **kwargs):
        '''
        Form for voting
        '''
        return self.wrap_form(ElectionVoteForm)(request, election, **kwargs)



    @permission_required('emit_direct_vote', (Election, 'id', 'electionid'))
    def cancel_vote_action(self, request, election, **kwargs):
        '''
        Cancels a vote
        '''

        election_url=election.get_link()
        vote = election.get_vote_for_voter(request.user)

        if not vote or not vote.is_direct:
            data = dict(errors=_('You didn\'t participate in this election.'))
            return self.raise_error(request, http.HttpBadRequest, data)

        vote.invalidated_at_date = datetime.datetime.now()
        vote.is_counted = False
        vote.save()

        context = get_base_email_context(request)
        context.update(dict(
            election=election,
            election_url=election_url,
            to=vote.voter,
            agora_url=election.agora.get_link()
        ))

        try:
            context['delegate'] = get_delegate_in_agora(vote.voter, election.agora)
        except:
            pass

        if request.user.get_profile().has_perms('receive_email_updates'):
            translation.activate(request.user.get_profile().lang_code)
            email = EmailMultiAlternatives(
                subject=_('Vote cancelled for election %s') % election.pretty_name,
                body=render_to_string('agora_core/emails/vote_cancelled.txt',
                    context),
                to=[vote.voter.email])

            email.attach_alternative(
                render_to_string('agora_core/emails/vote_cancelled.html',
                    context), "text/html")
            email.send()
            translation.deactivate()

        action.send(request.user, verb='vote cancelled', action_object=election,
            target=election.agora, ipaddr=request.META.get('REMOTE_ADDR'),
            geolocation=json.dumps(geolocate_ip(request.META.get('REMOTE_ADDR'))))

        return self.create_response(request, dict(status="success"))

    def get_all_votes(self, request, **kwargs):
        '''
        List all the votes in this agora
        '''
        return self.get_custom_resource_list(request, resource=CastVoteResource,
            queryfunc=lambda election: election.get_all_votes(), **kwargs)

    def get_cast_votes(self, request, **kwargs):
        '''
        List votes in this agora
        '''
        return self.get_custom_resource_list(request, resource=CastVoteResource,
            queryfunc=lambda election: election.cast_votes.all(), **kwargs)

    def get_delegated_votes(self, request, **kwargs):
        '''
        List votes in this agora
        '''
        return self.get_custom_resource_list(request, resource=CastVoteResource,
            queryfunc=lambda election: election.get_delegated_votes(), **kwargs)

    def get_votes_from_delegates(self, request, **kwargs):
        '''
        List votes in this agora
        '''
        return self.get_custom_resource_list(request, resource=CastVoteResource,
            queryfunc=lambda election: election.get_votes_from_delegates(), **kwargs)

    def get_direct_votes(self, request, **kwargs):
        '''
        List votes in this agora
        '''
        return self.get_custom_resource_list(request, resource=CastVoteResource, 
            queryfunc=lambda election: election.get_direct_votes(), **kwargs)

    def get_custom_resource_list(self, request, queryfunc, resource, **kwargs):
        '''
        List custom resources (mostly used for votes)
        '''
        election = None
        electionid = kwargs.get('electionid', -1)
        try:
            election = Election.objects.get(id=electionid)
        except:
            raise ImmediateHttpResponse(response=http.HttpNotFound())

        return resource().get_custom_list(request=request,
            queryset=queryfunc(election))


    def dehydrate_percentage_of_participation(self, bundle):
        return bundle.obj.percentage_of_participation()
