from . import app, celery, login_manager
from .helpers import (suggested_artists, random_catalog, seed_playlist)
from . import frontend_helpers
from config import BaseConfig

from flask.ext.login import login_user, logout_user
from flask.ext.login import UserMixin
from flask import render_template, request, redirect, url_for
from flask import session, flash

from flask.ext.login import login_user, logout_user, login_required, UserMixin
from flask.ext.wtf import Form
from flask import render_template, request, redirect, url_for, session, flash

import spotipy
import spotipy.util as util
import base64
import requests
import helpers
import db


def oauth_prep(config=None, scope=['user-library-read']):
    ''' Connect to Spotify using spotipy & our app config credentials.
    'scope' should be a list. Multiple scopes will be processed below. '''

    scope = ' '.join(scope)
    oauth = spotipy.oauth2.SpotifyOAuth(client_id=config.CLIENT_ID,
                                        client_secret=config.CLIENT_SECRET,
                                        redirect_uri=config.REDIRECT_URI,
                                        scope=scope)
    return oauth

scope = ['user-library-read', 'playlist-read-collaborative',
         'user-follow-read', 'playlist-modify-public']

oauth = oauth_prep(BaseConfig, scope)


class User(UserMixin):
    ''' User class used by Flask-Login to manage/track user's within 
    session cookie. Primary ID is the user's Spotify ID.'''

    users = {}

    def __init__(self, spotify_id, access_token, refresh_token, artists=set(),
                 search_results=None):
        self.id = unicode(spotify_id)
        self.access = access_token
        self.refresh = refresh_token
        self.users[self.id] = self

    @classmethod
    def get(cls, user_id):
        if cls.users:
            if user_id in cls.users:
                return cls.users[user_id]
        else:
            return None


@login_manager.user_loader
def load_user(user_id):
    ''' Retrieves user from User.users.'''
    return User.get(user_id)


class UserCache():
    ''' Global cache that's used to temporarily store a variety of different
    user data while they are interacting with site, including festival preferences
    and festival search results. A temporary data structure that will be replaced
    in the future with a sturdier MySQL schema.

    includes CRUD functions for storage of user preference's by festival. '''
    def __init__(self, artists=set(), hotness=None, danceability=None, enery=None,
                 energy=None, variety=None, adventurousness=None, organizer=0,
                 search_results=list(), festival_name=None):
        self.artists = {}
        self.hotness = hotness
        self.danceability = danceability
        self.energy = energy
        self.variety = variety
        self.adventurousness = adventurousness
        self.organizer = organizer
        self.search_results = search_results
        self.festival_name = festival_name
        self.user_id = None
        self.user_festivals = None
        self.did_user_sel_parameters = False
        self.festival_id = None

    def save_preferences(self, artists, urlSlug):
        if not isinstance(artists, set):
            raise TypeError('Artist data not a set object.')
        _current_user = str(session.get('user_id'))
        if not self.artists.get(_current_user):
            self.artists[_current_user] = {}
        self.artists[_current_user][urlSlug] = artists
        return

    def retrieve_preferences(self, urlSlug):
        _current_user = str(session.get('user_id'))
        if not self.artists.get(_current_user):
            return None
        elif not self.artists[_current_user].get(urlSlug):
            return None
        else:
            return self.artists[_current_user][urlSlug]

    def update_preferences(self, artists, urlSlug):
        _current_user = str(session.get('user_id'))
        if not isinstance(artists, set):
            raise TypeError('Artist data not a set object.')
        cur_preferences = self.artists[_current_user][urlSlug]
        app.logger.warning("Update pref - length before...{}".format(len(cur_preferences)))
        app.logger.warning("Update pref - {} about to be added to cur_preferences..".format(artists))
        self.artists[_current_user][urlSlug] = cur_preferences | artists
        app.logger.warning("Update pref -  length after...{}".format(
                           len(self.artists[_current_user][urlSlug])))
        return

    def delete_preferences(self):
        _current_user = str(session.get('user_id'))
        del self.artists[_current_user]
        return


def spotifest_logout():
    ''' Removes user from session cookie via Flask-Login's 
    logout_user function. User preferences are deleted from user_cache.'''
    if load_user(session.get('user_id')):
        user_cache.delete_preferences()
    logout_user()
    return

user_cache = UserCache()


@login_manager.needs_refresh_handler
def refresh():
    ''' Manages exchange of refresh_token for a new access_token, if
    a user is logged in, allowing user to stay logged in for a long time.
    '''
    current_user = load_user(session.get('user_id'))
    if current_user:
        re_auth_in = BaseConfig.CLIENT_ID + ':' + BaseConfig.CLIENT_SECRET
        re_auth = base64.b64encode(re_auth_in)
        headers = {'Authorization': 'Basic {}'.format(str(re_auth))}
        payload = {'grant_type': 'refresh_token',
                   'refresh_token': current_user.refresh}
        r = requests.post(oauth.OAUTH_TOKEN_URL, data=payload, headers=headers)
        new_access = r.json()['access_token']
        current_user.access = new_access
        for u in User.users.values():
            app.logger.warning(u.__dict__)
            #app.logger.warning("Current user:{}, {}".format(u.id, u.access))
    else:
        return redirect(url_for('home'))
    return


@app.before_request
def before_request():
    ''' Attempts to refresh user OAuth login by exchanging refresh token
    for new_access auth token. If login has gone stale, will simply logout. '''
    refresh()
    if session.get('user_id') and not load_user(session.get('user_id')):
        app.logger.warning("Refresh Failed; User '{}' not found "
                           "- possibly invalid token. Logging out".format(session.get('user_id')))
        spotifest_logout()
    return


def login(config=BaseConfig, oauth=oauth):
    ''' Returns URL that allows user to sign in with Spotify credentials. Uses
    spotipy's OAuth2 interface to populate request URL with payload specified
    in Spotify API.
    '''
    payload = {'client_id': oauth.client_id,
               'response_type': 'code',
               'redirect_uri': oauth.redirect_uri,
               'scope': oauth.scope}
    r = requests.get(oauth.OAUTH_AUTHORIZE_URL, params=payload)
    return r.url


@app.route('/', methods=['POST', 'GET'])
@app.route('/home', methods=['POST', 'GET'])
def home(config=BaseConfig):
    '''
    If no temporary code in request arguments, attempt to login user
    through Oauth.

    If there's a code (meaning successful sign-in to Spotify Oauth),
    and there is currently no users on the session cookie, go ahead and login
    the user to session.

    render home.html
    '''
    code = request.args.get('code')
    print "Code - ", code
    active_user = session.get('user_id')
    if request.method == 'GET':
        if not code and not active_user:
            auth_url = login()
            return render_template('home.html', login=False, oauth=auth_url)
        else:

            if not User.users or not session.get('user_id'):

                # log user to session (Flask-Login)
                response = oauth.get_access_token(request.args['code'])
                token = response['access_token']
                s = spotipy.Spotify(auth=token)
                user_id = s.me()['id']
                new_user = User(user_id, token, response['refresh_token'])
                login_user(new_user)
                app.logger.warning("NEW user login '{}'".format(new_user.id))
            # at this point, user is logged in, so if you click "Create"

            current_user = load_user(session.get('user_id')).access
            s = spotipy.Spotify(auth=current_user)

    user_cache.user_festivals = db.get_user_festivals(user_cache.user_id)

    if request.method == 'POST':
        url_slug = request.form['festival_id']
        url_slug = helpers.sanitize_url_slug(url_slug)
        app.logger.warning("ATTEMPTING JOIN Festival ID/urlSlug is '{}'".format(url_slug))
        return redirect(url_for('join', url_slug=url_slug))
    return render_template('home.html', login=True,
                            user_festivals=user_cache.user_festivals,
                            user_id=user_cache.user_id)


@app.route('/festival/join/<url_slug>', methods=['GET'])
@login_required
def join(url_slug):
    ''' Adds current logged-in user to festival located at
    url_slug. If user is a contributor, this information is
    saved to dB.
    '''
    current_festival = db.get_info_from_database(url_slug)
    if not current_festival:
        flash(("Festival '{}' does not exist! Please check"
               " the code and try again.").format(url_slug))
        return redirect(url_for('home'))
    organizer = current_festival[2]
    _user = session.get('user_id')
    app.logger.warning("User '{}' is joining festival '{}'".format(_user, url_slug))
    if organizer != _user:
        try:
            db.save_contributor(current_festival[0], _user)
        except:
            app.logger.warning("Contributor {} is already in the database.".format(_user))
    else:
        flash("Welcome back to your own festival!")
    return redirect(url_for('festival', url_slug=url_slug))


@app.route('/festival/create_new', methods=['GET'])
@login_required
def new():
    ''' Create a new festival (new_url_slug), gather user
    preferences of organizer, save festival to dB, save organizer
    to dB as a contributor and redirect to newly minted festival page.
    '''
    current_user = load_user(session.get('user_id'))
    new_url_slug = helpers.generate_urlslug(current_user.id)
    new_catalog = helpers.Catalog(new_url_slug, 'general')
    s = spotipy.Spotify(auth=current_user.access)

    processor = helpers.AsyncAdapter(app)
    user_cache.save_preferences(processor.get_user_preferences(s), new_url_slug)
    artists = user_cache.retrieve_preferences(new_url_slug)

    if user_cache.retrieve_preferences(new_url_slug):
        processor.populate_catalog(artists, 3, catalog=new_catalog)

    db.save_to_database(None, current_user.id, None, None,
                        new_catalog.id, new_url_slug)

    current_festival = db.get_info_from_database(new_url_slug)
    festivalId = current_festival[0]
    userId = current_festival[2]
    if app.config['IS_ASYNC']:
        db.save_contributor.apply_async(args=(festivalId, userId),
                                        kwargs={'organizer': 1, 'ready': 1})
    else:
        db.save_contributor(festivalId, userId, organizer=1, ready=1)
    app.logger.warning("NEW festival created at '{}'".format(new_url_slug))
    return redirect(url_for('festival', url_slug=new_url_slug))


@app.route('/festival/<url_slug>', methods=['GET', 'POST'])
@login_required
def festival(url_slug):
    '''
    Fetches a festival page based on the url_slug provided:
     - retrieve contributor information from dB
     - retrieve artist preferences of current user from dB or cache
     - prepares page forms that populate page
    '''
    current_festival = db.get_info_from_database(url_slug)
    user_cache.cur_festival_id = current_festival[0]
    if not current_festival:
        flash(("Festival '{}' does not exist! Please check"
               "the code and try again.").format(url_slug))
        return redirect(url_for('home'))

    organizer = current_festival[2]
    _user = session.get('user_id')
    app.logger.warning("User '{}' accessing festival '{}'".format(_user,
                                                                   url_slug))
    is_org = True
    # check if organizer & if so, find name
    if organizer != _user:
        is_org = False
        if current_festival[1]:
            festival_name = current_festival[1]
        else:
            festival_name = "Spotifest 2016"
    elif organizer == _user:
        is_org = True
        festival_name = None
    # fetch contributors: the 0th term = the main organizer!
    try:
        all_users = db.get_contributors(current_festival[0])
        app.logger.warning("All users in this festival -- '{}'".format(all_users))
        if all_users is None:
            flash(("Festival '{}' is having problems. Please check with the "
                   "organizer. Try again later or create a new festival!").format(url_slug))
            return redirect(url_for('home'))
    except:
        app.logger.warning("Couldn't find contributors - check DB functions or app code.")
        flash(("Festival '{}' is having problems. Please check with the "
               "organizer. Try again later or create a new festival!.").format(url_slug))
        return redirect(url_for('home'))

    new = None
    new_artist = None

    current_user = load_user(session.get('user_id')).access
    s = spotipy.Spotify(auth=current_user)
    try:
        if not user_cache.retrieve_preferences(url_slug):
            processor = helpers.AsyncAdapter(app)
            artists = processor.get_user_preferences(s)
            user_cache.save_preferences(artists, url_slug)
        else:
            app.logger.warning("Current # of artists for "
                               "user '{}' - '{}'".format(_user, len(user_cache.retrieve_preferences(url_slug))))
    except:
        app.logger.warning("No artists followed found in the user's Spotify account.")

    # prep forms
    searchform = frontend_helpers.SearchForm()
    suggested_pl_butt = frontend_helpers.SuggestedPlaylistButton()
    art_select = frontend_helpers.ArtistSelect(request.form)
    params_form = frontend_helpers.ParamsForm()

    saved_params = db.get_parameters(_user, url_slug)
    frontend_helpers.populate_params(params_form, saved_params)

    if searchform.validate_on_submit():
        s_artist = searchform.artist_search.data
        user_cache.search_results = helpers.search_artist_echonest(s_artist)
        art_select.artist_display.choices = user_cache.search_results
        app.logger.warning("Search results '{}'".format(user_cache.search_results))

    if request.form.get("selectartist"):
        chosen_art = request.form.get("selectartist")
        cur_user_preferences = user_cache.retrieve_preferences(url_slug)
        new_artist = chosen_art

        if not cur_user_preferences or chosen_art not in cur_user_preferences:
            app.logger.warning("Adding chosen artist.. {}".format(chosen_art))
            user_cache.update_preferences(set([chosen_art]), url_slug)
            new_artist = chosen_art
            new = 1
        else:
            new = 0
        user_cache.search_results = list()

    if suggested_pl_butt.validate_on_submit():
        if request.form.get("add_button"):
            user_cache.update_preferences(helpers.suggested_artists, url_slug)
            new = True

    return render_template('festival.html', url_slug=url_slug,
                           s_results=user_cache.search_results,
                           art_select=art_select, searchform=searchform,
                           suggested_pl_butt=suggested_pl_butt,
                           artists=user_cache.artists,
                           params_form=params_form,
                           all_users=all_users,
                           festival_name=festival_name,
                           user=_user,
                           new=new, new_artist=new_artist, is_org=is_org)


@app.route('/festival/<url_slug>/update_parameters', methods=['POST'])
def update_parameters(url_slug):
    ''' Get current user, and save their festival parameters
    for to dB.

    Used by contributors after 'proposing vision'.
    '''
    _user = session.get('user_id')
    current_festival = db.get_info_from_database(url_slug)
    festivalId = current_festival[0]
    catalog_id = current_festival[5]
    catalog = helpers.Catalog(catalog_id)
    artists = user_cache.retrieve_preferences(url_slug)

    get_festival = db.get_info_from_database(url_slug)
    festivalId = get_festival[0]
    festival_org = get_festival[2]
    h = request.form.get('hotttnesss')
    d = request.form.get('danceability')
    e = request.form.get('energy')
    v = request.form.get('variety')
    a = request.form.get('adventurousness')
    if _user == festival_org:
        name = request.form.get('name')
        db.update_festival(name, url_slug)
    db.update_parameters(festivalId, _user, h, d, e, v, a)

    if artists:
        processor = helpers.AsyncAdapter(app)
        processor.populate_catalog(artists, 3, catalog=catalog)
        flash_message = ("You've pitched the perfect festival to the organizer." +
                         " Now we wait.")
    else:
        flash_message = "You haven't contributed artists, but your style is pitched!"
    flash(flash_message)
    app.logger.warning("VISION PROPOSED - '{}'' has saved"
                       " parameters at festival '{}'".format(_user, festivalId))
    return redirect(url_for('festival', url_slug=url_slug))


@app.route('/festival/<url_slug>/results', methods=['POST', 'GET'])
def results(url_slug):
    ''' Based on all inputs (parameters, artist preferences) from
    all contributors in a festival, a new playlist is processed and
    generated - leading user to a page with the embeddable Spotify playlist
    '''
    current_festival = db.get_info_from_database(url_slug)
    festival_catalog = current_festival[5]

    if request.method == 'POST':
        # Did user click on join festival ?
        try:
            if request.form['festival_id']:
                app.logger.warning('User selected join (Why is this here?)')
                auth_url = login()
                app.logger.warning("Just 'logged' in by generating festival?")
                user_cache.festival_id = request.form['festival_id']
                return redirect(auth_url)
        except:
            if user_cache.festival_id is None:
                app.logger.warning('User did not click on join and selected parameter')
            else:
                app.logger.warning('User selected parameters')

        if not user_cache.artists:
            flash("You really should add some artists!"
                  " Maybe you can use our suggestions..")
            return redirect(url_for('home'))

        # parameters
        enough_data = True
        name = request.form.get('name')
        h = request.form.get('hotttnesss')
        d = request.form.get('danceability')
        e = request.form.get('energy')
        v = request.form.get('variety')
        a = request.form.get('adventurousness')
        user_cache.did_user_sel_parameters = True
        current_user = load_user(session.get('user_id')).access
        # db.update_parameters(festivalId, _user, h, d, e, v, a)
        s = spotipy.Spotify(auth=current_user)
        user_id = s.me()['id']

        # db.get_average_parameters(user_cache.current_festival)
        processor = helpers.AsyncAdapter(app)
        playlist = helpers.seed_playlist(catalog=festival_catalog, hotttnesss=h,
                                         danceability=d, energy=e, variety=v,
                                         adventurousness=a)
        songs_id = processor.process_spotify_ids(50, 10, s, playlist)

        if user_cache.festival_id is not None and user_cache.did_user_sel_parameters:

            festival_information = db.get_info_from_database(user_cache.festival_id)
            playlist_url = festival_information[4]
            id_playlist = festival_information[3]
            helpers.add_songs_to_playlist(s, user_id, id_playlist, songs_id)
            return render_template('results.html', playlist_url=playlist_url,
                                   enough_data=enough_data)
        else:
            helpers.create_playlist(s, user_id, name)
            id_playlist = helpers.get_id_from_playlist(s, user_id, name)
            helpers.add_songs_to_playlist(s, user_id, id_playlist, songs_id)
            u_id = str(user_id)
            id_pl = str(id_playlist)
            playlist_url = ('https://embed.spotify.com/?uri=spotify:user:'
                            '{}:playlist:{}'.format(u_id, id_pl))
            if app.config['IS_ASYNC'] is True:
                db.update_festival.apply_async(args=[name, url_slug, id_playlist,
                                               playlist_url])
            else:
                db.update_festival(name, url_slug, id_playlist, playlist_url)
            app.logger.warning("Playlist for festival '{}'"
                               "successfully generated".format(user_cache.festival_id))
            return render_template('results.html', playlist_url=playlist_url,
                                   enough_data=enough_data)


@app.route('/about')
def about():
    ''' Renders about page. '''
    return render_template('about.html')


@app.errorhandler(401)
def access_blocked(error):
    ''' If user is not logged in, redirect to home and flash message.'''
    auth_url = login()
    flash('Please login with your Spotify account before continuing!')
    return render_template('home.html', login=False, oauth=auth_url)
