import itertools
import json

from asgiref.inmemory import ChannelLayer
from channels import Channel, Group
from channels.auth import channel_session_user, channel_session_user_from_http
from django.db import transaction
from django.utils import timezone

from ..core.config import config
from ..core.models import Projector
from ..users.auth import AnonymousUser
from ..users.models import User
from .collection import Collection, CollectionElement


def get_logged_in_users():
    """
    Helper to get all logged in users.

    Only works with the OpenSlides session backend.
    """
    return User.objects.exclude(session=None).filter(session__expire_date__gte=timezone.now()).distinct()


@channel_session_user_from_http
def ws_add_site(message):
    """
    Adds the websocket connection to a group specific to the connecting user.

    The group with the name 'user-None' stands for all anonymous users.
    """
    Group('user-{}'.format(message.user.id)).add(message.reply_channel)


@channel_session_user
def ws_disconnect_site(message):
    """
    This function is called, when a client on the site disconnects.
    """
    Group('user-{}'.format(message.user.id)).discard(message.reply_channel)


@channel_session_user_from_http
def ws_add_projector(message, projector_id):
    """
    Adds the websocket connection to a group specific to the projector with the given id.
    Also sends all data that are shown on the projector.
    """
    user = message.user
    # user is the django anonymous user. We have our own.
    if user.is_anonymous and config['general_systen_enable_anonymous']:
        user = AnonymousUser()

    if not user.has_perm('core.can_see_projector'):
        message.reply_channel.send({'text': 'No permissions to see this projector.'})
    else:
        try:
            projector = Projector.objects.get(pk=projector_id)
        except Projector.DoesNotExist:
            message.reply_channel.send({'text': 'The projector {} does not exist.'.format(projector_id)})
        else:
            # At first, the client is added to the projector group, so it is
            # informed if the data change.
            Group('projector-{}'.format(projector_id)).add(message.reply_channel)

            # Send all elements that are on the projector.
            output = []
            for requirement in projector.get_all_requirements():
                required_collection_element = CollectionElement.from_instance(requirement)
                output.append(required_collection_element.as_autoupdate_for_projector())

            # Send all config elements.
            collection = Collection(config.get_collection_string())
            output.extend(collection.as_autoupdate_for_projector())

            # Send the projector instance.
            collection_element = CollectionElement.from_instance(projector)
            output.append(collection_element.as_autoupdate_for_projector())

            # Send all the data that was only collected before
            message.reply_channel.send({'text': json.dumps(output)})


def ws_disconnect_projector(message, projector_id):
    """
    This function is called, when a client on the projector disconnects.
    """
    Group('projector-{}'.format(projector_id)).discard(message.reply_channel)


def send_data(message):
    """
    Informs all site users and projector clients about changed data.
    """
    collection_element = CollectionElement.from_values(**message)

    # Loop over all logged in site users and the anonymous user and send changed data.
    for user in itertools.chain(get_logged_in_users(), [AnonymousUser()]):
        channel = Group('user-{}'.format(user.id))
        output = collection_element.as_autoupdate_for_user(user)
        channel.send({'text': json.dumps([output])})

    # Loop over all projectors and send data that they need.
    for projector in Projector.objects.all():
        if collection_element.is_deleted():
            output = collection_element.as_autoupdate_for_projector()
        else:
            information = message.get('information', {})
            collection_elements = projector.get_collections_required_for_this(collection_element, **information)
            output = [collection_element.as_autoupdate_for_projector() for collection_element in collection_elements]
        if output:
            Group('projector-{}'.format(projector.pk)).send(
                {'text': json.dumps(output)})


def inform_changed_data(instance, information=None):
    """
    Informs the autoupdate system and the caching system about the creation or
    update of an element.
    """
    try:
        root_instance = instance.get_root_rest_element()
    except AttributeError:
        # Instance has no method get_root_rest_element. Just ignore it.
        pass
    else:
        collection_element = CollectionElement.from_instance(
            root_instance,
            information=information)
        # If currently there is an open database transaction, then the
        # send_autoupdate function is only called, when the transaction is
        # commited. If there is currently no transaction, then the function
        # is called immediately.
        transaction.on_commit(lambda: send_autoupdate(collection_element))


def inform_deleted_data(collection_string, id, information=None):
    """
    Informs the autoupdate system and the caching system about the deletion of
    an element.
    """
    collection_element = CollectionElement.from_values(
        collection_string=collection_string,
        id=id,
        deleted=True,
        information=information)
    # If currently there is an open database transaction, then the
    # send_autoupdate function is only called, when the transaction is
    # commited. If there is currently no transaction, then the function
    # is called immediately.
    transaction.on_commit(lambda: send_autoupdate(collection_element))


def send_autoupdate(collection_element):
    """
    Helper function, that sends a collection_element through a channel to the
    autoupdate system.
    """
    try:
        Channel('autoupdate.send_data').send(collection_element.as_channels_message())
    except ChannelLayer.ChannelFull:
        pass
