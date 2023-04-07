import cherrypy
import errno

from girder import plugin

from girder.api.describe import Description, autoDescribeRoute
from girder.api import access 
from girder.api.rest import boundHandler
from girder.constants import AccessType, TokenScope

from girder.models.file import File as FileModel
from girder.models.upload import Upload
from girder.utility import RequestBodyStream
from girder.utility.model_importer import ModelImporter
from girder.exceptions import GirderException

@access.public(cookie=True, scope=TokenScope.DATA_WRITE)
@boundHandler
@autoDescribeRoute(
    Description('Save VolView session in an item')
    .param('itemId', 'The item ID', paramType='path')
    .errorResponse())
def saveVolView(self, itemId):
    size = int(cherrypy.request.headers.get('Content-Length'))

    # modified from girder.api.v1.file.File.initUpload 
    fileModel = FileModel()
    parentId = itemId
    parentType = 'item'
    name = 'session.volview.zip'
    mimeType = 'application/zip'
    reference = None
    user = self.getCurrentUser()
    parent = ModelImporter.model(parentType).load(
        id=parentId, user=user, level=AccessType.WRITE, exc=True)

    assetstore = None

    chunk = None
    if size > 0 and cherrypy.request.headers.get('Content-Length'):
        ct = cherrypy.request.body.content_type.value
        if (ct not in cherrypy.request.body.processors
                and ct.split('/', 1)[0] not in cherrypy.request.body.processors):
            chunk = RequestBodyStream(cherrypy.request.body)
    if chunk is not None and chunk.getSize() <= 0:
        chunk = None

    try:
        upload = Upload().createUpload(
            user=user, name=name, parentType=parentType, parent=parent, size=size,
            mimeType=mimeType, reference=reference, assetstore=assetstore)
    except OSError as exc:
        if exc.errno == errno.EACCES:
            raise GirderException(
                'Failed to create upload.', 'girder.api.v1.file.create-upload-failed')
        raise
    if upload['size'] > 0:
        if chunk:
            return Upload().handleChunk(upload, chunk, filter=True, user=user)

        return upload
    else:
        return fileModel.filter(Upload().finalizeUpload(upload), user)

class GirderPlugin(plugin.GirderPlugin):
    DISPLAY_NAME = 'VolView'
    CLIENT_SOURCE_PATH = 'web_client'

    def load(self, info):
        info['apiRoot'].item.route('POST', (':itemId', 'volview'), saveVolView)

