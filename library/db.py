import base64
from library.app import app, mysql, celery


@celery.task(name='save_to_database')
def save_to_database(festivalName, userId, playlistId, playlistURL, catalogId):
    '''
    saves infromation the data base.
    festivalId will be created automatically
    '''
    festivalName = str(festivalName)
    userId = str(userId)
    playlistId = str(playlistId)
    playlistURL = str(playlistURL)
    catalogId = str(catalogId)
    url_slug = str(base64.b64encode(festivalName + playlistURL))[:7]
    values = (festivalName, userId, playlistId, playlistURL, catalogId, url_slug)
    with app.app_context():
        connection = mysql.connect()
        cursor = connection.cursor()
        cursor.execute("INSERT INTO sessions (festivalName, userId, playlistId, playlistURL, catalogId, urlSlug) VALUES (%s, %s, %s, %s, %s, %s)", values)
        connection.commit()
        print 'saved to database'
    return


def save_contributor(festivalId, userId):
    festivalId = int(festivalId)
    userId = str(userId)
    values = (festivalId, userId)
    with app.app_context():
        connection = mysql.connect()
        cursor = connection.cursor()
        cursor.execute("INSERT INTO contributors VALUES (%s, %s)", values)
        connection.commit()
        print 'saved to database'
    return



def get_info_from_database(festivalId):
    '''
    return a list with all the information from the
    database for a certain festival id
    '''
    connection = mysql.get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM sessions WHERE festivalId = %s", (festivalId,))
    data = cursor.fetchall()
    festivalId = int(data[0][0])
    userId = str(data[0][1])
    playlistId = str(data[0][2])
    playlistURL = str(data[0][3])
    catalogId = str(data[0][4])
    values = [festivalId, userId, playlistId, playlistURL, catalogId]
    return values


def get_contributors(festivalId):
    '''
    return a list with all the contributors id of
    the festival
    '''
    print type(festivalId)
    connection = mysql.get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT userId FROM contributors WHERE festivalId = %s", (festivalId,))
    data = cursor.fetchall()
    users = []
    for user in data:
        users.append(user[0].encode('utf-8'))
    return  users
