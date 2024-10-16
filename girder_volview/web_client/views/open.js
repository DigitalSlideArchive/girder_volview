import { getApiRoot } from "@girder/core/rest";

const openButton = `<a class="btn btn-sm btn-primary open-in-volview hidden" style="margin-left: 10px" role="button">
                                <i class="icon-link-ext"></i>Open in VolView</a>`;

export function addButton($el, siblingSelector) {
    const sibling = $el.find(siblingSelector);
    if (!sibling.length) {
        console.warn(
            `Tried to add VolView button, but sibling element not found with selector: ${siblingSelector}`
        );
        return;
    }
    sibling.before(openButton);
    const button = $el.find(".open-in-volview")[0];
    return button;
}

const volViewPath = `static/built/plugins/volview/index.html`;

export function openItemURL(item) {
    const itemRoute = `/${getApiRoot()}/item/${item.id}`;
    const saveParam = `&save=${itemRoute}/volview`;
    const manifestUrl = `${itemRoute}/volview`;
    const downloadParams = `&names=[manifest.json]&urls=${manifestUrl}`;
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    return newTabUrl;
}

export function openItem(item) {
    window.open(openItemURL(item), "_blank").focus();
}

function resourcesToDownloadParams(folderId, resources) {
    const items = (resources.item || []).join(",");
    const folders = (resources.folder || []).join(",");
    const manifestUrl = `/${getApiRoot()}/folder/${folderId}/volview?folders=${folders}&items=${items}`;
    return `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
}

export function openResourcesURL(folder, resources) {
    const folderRoute = `/${getApiRoot()}/folder/${folder.id}`;
    const metaData = {
        linkedResources: {
            items: resources.item,
            folders: resources.folder,
        },
    };
    const saveParam = `&save=${folderRoute}/volview?metadata=${encodeURIComponent(
        JSON.stringify(metaData)
    )}`;
    const downloadParams = resourcesToDownloadParams(folder.id, resources);
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    return newTabUrl;
}

export function openResources(folder, resources) {
    window.open(openResourcesURL(folder, resources), "_blank").focus();
}

export function openGroupedItemURL(item, folder) {
    const folderId = folder ? folder.id : item.get('folderId');
    const folderRoute = `/${getApiRoot()}/folder/${folderId}`;
    const groups = item.get('meta')._grouping || {};
    const filter = {};
    (groups.keys || []).forEach((key, idx) => {
        if ((groups.values || [])[idx] !== undefined) {
            filter[key] = groups.values[idx];
        }
    });
    const metaData = {linkedResources: {filter: filter}};
    const saveParam = `&save=${folderRoute}/volview?metadata=${encodeURIComponent(
        JSON.stringify(metaData)
    )}`;
    const manifestUrl = `/${getApiRoot()}/folder/${folderId}/volview?filters=${JSON.stringify(filter)}`;
    const downloadParams = `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    return newTabUrl;
}
