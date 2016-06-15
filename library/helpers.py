import random
import spotipy
import spotipy.util as util

from flask import Flask
from pyechonest import config
from pyechonest import playlist
from pyechonest import artist
from pyechonest.catalog import Catalog

from celery import group
from . import celery

from config import BaseConfig
import os
import re
import datetime
import base64
import hashlib
import logging


logger = logging.getLogger('library')

config.ECHO_NEST_API_KEY = BaseConfig.ECHONEST_API_KEY

suggested_artists = set(['Radiohead', 'Nirvana', 'The Beatles', 'David Bowie',
                        'Aretha Franklin', 'Mogwai', 'Eels', 'Glass Animals',
                        'Grimes', 'Sungrazer', 'Queens of the Stone Age'])


class AsyncAdapter(object):
    '''
    an adapter class that encapsulates helper functions that have asynchronous
    options. The following helpers can be processed in via celery.
        - process_spotify_ids()
        - get_user_preferences()
    '''

    def __init__(self, app):
        if 'IS_ASYNC' not in app.config:
            raise KeyError("Please set config key 'IS_ASYNC' to True | False.")
        self.is_async = app.config['IS_ASYNC']

    def process_spotify_ids(self, total_items, chunk_size, spotipy, playlist):
        if not self.is_async:
            return self.non_async_process_spotify_ids(spotipy, playlist)
        else:
            return self.async_process_spotify_ids(total_items, chunk_size,
                                                  spotipy, playlist)

    def get_user_preferences(self, spotipy):
        '''
        Returns set of artists gathered from a spotify user's saved tracks,
        public playlists and followed artists.
        '''
        if not self.is_async:
            return self.non_async_get_user_preferences(spotipy)
        else:
            return self.async_get_user_preferences(spotipy)

    def populate_catalog(self, artists, num_tasks, limit=5, catalog=None):
        '''
        Populates a given catalog object with a randomized selection of songs.
        '''
        if not self.is_async:
            return self.non_async_populate_catalog(artists, catalog)
        else:
            return self.async_populate_catalog(artists, num_tasks,
                                               limit, catalog)

    def non_async_process_spotify_ids(self, spotipy, playlist):
        '''
        Returns array of spotify song IDs, given an iterable of song names (playlist).
        '''
        songs_id = get_songs_id(spotipy, playlist, None)
        return songs_id

    def async_process_spotify_ids(self, total_items, chunk_size, spotipy, playlist):
        '''
        Asynchronous task factory, to run multiple task instances for
        the .get_songs_id(...) helper function.
        '''
        task_ids = []
        limit = total_items + chunk_size
        all_ids = group(get_songs_id.s(spotipy, playlist, chunk)
                        for chunk in xrange(0, limit, chunk_size))()
        return reduce(lambda x, y: x + y, all_ids.get())

    def non_async_get_user_preferences(self, spotipy):
        '''
        Normal wrapper for three helper functions involved in gathering
        user preferences.
        '''
        # artists from saved tracks
        st = get_user_saved_tracks(spotipy)
        # artists form user playlists (public)
        up = get_user_playlists(spotipy)
        # artists from followed artists
        fa = get_user_followed(spotipy)
        return st | up | fa

    def async_get_user_preferences(self, spotipy):
        '''
        Asynchronous task factory for three helper functions:
            - .get_user_saved_tracks(...)
            - .get_user_playlists(...)
            - .get_user_followed(...)
        '''
        tasks = []
        pref_funcs = [get_user_saved_tracks,
                      get_user_playlists,
                      get_user_followed]
        # artists from saved tracks
        preferences = group(func.s(spotipy) for func in pref_funcs)()
        return reduce(lambda x, y: x | y, preferences.get())

    def async_populate_catalog(self, artists, num_tasks, limit=5, catalog=None):
        '''
        Asynchronous task factory for the .populate_catalog(...) helper function.
        '''
        artists = list(artists)
        tasks = []
        if not catalog:
            random_catalog(artists, limit=15)
            return

        populated = group(random_catalog.s(artists, limit=5, catalog=catalog)
                          for _ in xrange(num_tasks))()

        if populated.successful():
            logger.warning('Catalog now has'
                           '{} items.'.format(len(catalog.get_item_dicts(results=100))))
        return

    def non_async_populate_catalog(self, artists, catalog):
        limit = 15
        return random_catalog(artists, limit, catalog)


@celery.task(name='saved_tracks')
def get_user_saved_tracks(spotipy):
    '''
    Returns a set of all artists found
    within the current Spotify user's saved tracks.
    '''
    offset = 0  # this set will be deleted if later we returns tracks instead of artists
    artists = set()
    while True:
        albums = spotipy.current_user_saved_tracks(limit=50, offset=offset)
        if not albums['items']:
            break
        batch = {item['track']['artists'][0]['name']
                    for item in albums['items']}
        artists.update(batch)

        offset += len(albums['items'])
    return artists


@celery.task(name='saved_playlists')
def get_user_playlists(spotipy):
    '''
    Returns set of all artists found
    within the current Spotify user's playlists.
    '''
    def show_tracks(results):
        '''
        helper function for get_user_playlists()
        '''
        for i, item in enumerate(tracks['items']):
            track = item['track']
            playlist_artists_list.append(track['artists'][0]['name'])

    playlist_artists_list = []
    offset = 0
    user_id = spotipy.current_user()['id']
    playlists = spotipy.user_playlists(user_id)

    for playlist in playlists['items']:
        owner = playlist['owner']['id']
        results = spotipy.user_playlist(owner, playlist['id'],
                                        fields="tracks,next")
        tracks = results['tracks']
        show_tracks(tracks)
        while tracks['next']:
            tracks = spotipy.next(tracks)
    return set(playlist_artists_list)


@celery.task(name='followed_users')
def get_user_followed(spotipy):
    '''
    Return a set of artists followed by the current user on Spotify.
    '''
    followed = spotipy.current_user_followed_artists()
    artists = {artist['name'] for artist in followed['artists']['items']}
    return artists


def search_artist_echonest(name):
    '''
    Returns array of artists based on search query to Echonest API.
    '''
    # add validation via echonest here
    results = artist.search(name=name)
    if results is False:
        return results
    else:
        results = sorted([art.name for art in results])
    return results


def create_playlist(spotipy, user_id, name_playlist):
    '''
    Creates a spotify playlist for user at user_id.
    '''
    spotipy.user_playlist_create(user_id, name_playlist, public=True)
    logger.warning("..... Playlist created")
    return


def add_songs_to_playlist(spotipy, user_id, playlist_id, id_songs):
    '''
    Inserts multiple songs (based on their spotify IDs)
    into a specified playlist.
    '''
    spotipy.user_playlist_add_tracks(user_id, playlist_id, id_songs)
    logger.warning("..... All songs added to playlist.")
    return


def get_id_from_playlist(spotipy, user_id, name_playlist):
    '''
    Returns spotify playlist ID, given a user_id and playlist name.
    '''
    offset = 0
    playlists = spotipy.user_playlists(user_id)
    user_playlists = {}  # This will stored the users playlists
    for playlist in playlists['items']:
        if playlist['name'] == name_playlist:
            return playlist['id']
    logger.warning('..... Could not find ID of new playlist.')
    return 


def insert_to_catalog(catalog, item):
    '''
    Wraps process_to_item funciton, returns catalog status ticket.
    '''
    ready = process_to_item(item)
    ticket = catalog.update(ready)
    return ticket


def process_to_item(artist):
    ''' Converts artist or song object into a formatted
    item to be inserted into a Catalog object.'''
    item = [{}]
    item[0]['action'] = 'update'
    item[0]['item'] = {}
    item[0]['item']['artist_name'] = artist
    return item


def seed_playlist(catalog, danceability=0.5, hotttnesss=0.5,
                  energy=0.5, variety=0.5, adventurousness=0.5,
                  results=50):
    '''
    Seed playlist and return playlist parameterized by args.
    '''
    pl = playlist.static(type='catalog-radio', seed_catalog=catalog,
                         min_danceability=danceability, artist_min_hotttnesss=hotttnesss,
                         min_energy=energy, variety=variety, adventurousness=adventurousness,
                         distribution='focused', artist_pick='song_hotttnesss-desc',
                         sort='artist_familiarity-desc', results=results)
    logger.warning('..... Songs in playslist: {}'.format(len(pl)))
    return pl


@celery.task(name='random_catalog')
def random_catalog(artists, limit=15, catalog=None):
    '''
    Inserts a number of artists into a catalog object based on
    random selection from iterable of artists.
    '''
    if not catalog:
        catalog = Catalog('your_catalog', 'general')
    artists = list(artists)
    for _ in xrange(limit):
        choice = random.choice(artists)
        artists.remove(choice)
        insert_to_catalog(catalog, choice)
    logger.warning('..... Catalog (or catalog chunk) generated')
    return catalog


@celery.task(name='song_ids')
def get_songs_id(spotipy, playlist, offset):
    '''
    Returns list of song IDs for each song in echonest playlist object.
    '''
    songs_id = []
    # full playlist
    if offset is None:
        playlist = playlist
    # playlist chunk
    elif isinstance(offset, int):
        playlist = playlist[offset:offset + 10]
    for item in playlist:
        q = "track:{} artist:{}".format(item.title.encode('utf-8'),
                                        item.artist_name.encode('utf-8'))

        result = spotipy.search(q, type='track', limit=1)
        if not result['tracks'].get('items'):
            continue
        spotify_id = spotipy.search(q, type='track', limit=1)['tracks']['items'][0]['id']
        songs_id.append(spotify_id)
    return songs_id


def generate_urlslug(user_id):
    ''' Create URL slug based on md5 hash of: user_id,
    current time, and unique base64 3 byte tag.
    '''
    unique = base64.b64encode(os.urandom(3))
    slug_hash = hashlib.md5(user_id + str(datetime.datetime.now()) + unique)
    new_url_slug = slug_hash.hexdigest()[:7]
    return new_url_slug


def sanitize_url_slug(url_slug):
    ''' Miscellaneous helper to sort out non-alphanumeric
    characters in input. Intended for use for auth.join
    '''
    if url_slug.isalnum():
        return url_slug
    process = re.findall(r'[A-Za-z0-9]', url_slug, re.I)
    sanitized = ''.join(process)
    return sanitized

