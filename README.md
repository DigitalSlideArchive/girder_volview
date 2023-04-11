# Girder Plugin for VolView

Open items in VolView with a "Open in VolView" button. The button is located in the top right, on an Item's page.

## Endpoints

* POST item/:id/volview -> upload file to Item with cookie authentication
* GET item/:id/volview -> download latest session.volview.zip
* GET item/:id/volview/datasets -> download all files in item except the \*.volview.zip

## Example Saving Roundtrip flow

1. User clicks Open in VolView for Item - Plugin checks if `*volview.zip` file exists in Item, finds none:
   Opens VolView with file download url `item/:id/volview/datasets`
1. VolView opens, fetches from `item/:id/volview/datasets`, receives zip of all files in Item except files ending in `*volview.zip`
1. In VolView, User clicks save button - VolView POSTs session.volview.zip to `item/:id/volview`
1. girder_volview Plugin saves new session.volview.zip in item.
1. User clicks Open in VolView for Item - Plugin finds a latests session.volview.zip in Item. Opens VolView with file download url `item/:id/volview`
1. VolView opens, fetches from `item/:id/volview`, receives most recently created `*volview.zip` file in Item.
