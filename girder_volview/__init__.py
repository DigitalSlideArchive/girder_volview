from girder import plugin

from girder.api.describe import Description, autoDescribeRoute
from girder.api import access 
from girder.api.v1.file import File 
from girder.constants import AccessType, TokenScope

@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@autoDescribeRoute(
    Description('Save VolView session with an item')
    .param('id', 'The item ID', paramType='path')
    .errorResponse())
def saveVolView(id):
    return File().initUpload('item', id, 'session.volview.zip', 1, 'application/zip')

class GirderPlugin(plugin.GirderPlugin):
    DISPLAY_NAME = 'VolView'
    CLIENT_SOURCE_PATH = 'web_client'

    def load(self, info):
        info['apiRoot'].item.route('POST', (':id', 'volview'), saveVolView)

