import { getApiRoot } from "@girder/core/rest";

const openButton = `<a class="btn btn-sm btn-primary open-in-volview hidden" style="margin-left: 10px" role="button">
                                <i class="icon-link-ext"></i>Open in VolView</a>`;

export function addButton($el, parentSelector) {
    const parent = $el.find(parentSelector);
    if (!parent.length) {
        console.warn(
            `Tried to add VolView button, but parent element not found with selector: ${parentSelector}`,
        );
        return;
    }
    parent.prepend(openButton);
    const button = $el.find(".open-in-volview")[0];
    return button;
}

const volViewPath = `static/built/plugins/volview/index.html`;

// Launch URL legs: `urls=` loads exactly what was picked, except a bare
// folder-open, which resumes the folder's newest session.volview.zip. `save=`
// returns a `resumeUrl` the client repoints `urls=` at, so a later F5 reloads
// the last save. `config=` delivers the folder config that registers the
// processing provider; without it the Jobs tab never appears.

function configParam(folderId) {
    const configUrl = `/${getApiRoot()}/folder/${folderId}/volview_config/.volview_config.yaml`;
    return `&config=${encodeURIComponent(configUrl)}`;
}

export function openItemURL(item) {
    const itemRoute = `/${getApiRoot()}/item/${item.id}`;
    const saveParam = `&save=${itemRoute}/volview`;
    const manifestUrl = `${itemRoute}/volview`;
    const downloadParams = `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}${configParam(
        item.get("folderId"),
    )}`;
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
        JSON.stringify(metaData),
    )}`;
    const downloadParams = resourcesToDownloadParams(folder.id, resources);
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}${configParam(
        folder.id,
    )}`;
    return newTabUrl;
}

export function openResources(folder, resources) {
    window.open(openResourcesURL(folder, resources), "_blank").focus();
}

export function groupingFilterForItem(item) {
    const groups = (item.get("meta") || {})._grouping || {};
    const filter = {};
    (groups.keys || []).forEach((key, idx) => {
        if ((groups.values || [])[idx] !== undefined) {
            filter[key] = groups.values[idx];
        }
    });
    return filter;
}

function volViewURLWithFilter(folderId, filterPayload) {
    const folderRoute = `/${getApiRoot()}/folder/${folderId}`;
    const metaData = { linkedResources: { filter: filterPayload } };
    const saveParam = `&save=${folderRoute}/volview?metadata=${encodeURIComponent(
        JSON.stringify(metaData),
    )}`;
    const manifestUrl = `/${getApiRoot()}/folder/${folderId}/volview?filters=${encodeURIComponent(
        JSON.stringify(filterPayload),
    )}`;
    const downloadParams = `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
    return `${volViewPath}?${saveParam}${downloadParams}${configParam(folderId)}`;
}

export function openGroupedItemURL(item, folder) {
    const folderId = folder ? folder.id : item.get("folderId");
    return volViewURLWithFilter(folderId, groupingFilterForItem(item));
}

export function openCheckedGroupedURL(folder, filterList) {
    return volViewURLWithFilter(folder.id, filterList);
}

export function openCheckedGrouped(folder, filterList) {
    window.open(openCheckedGroupedURL(folder, filterList), "_blank").focus();
}
