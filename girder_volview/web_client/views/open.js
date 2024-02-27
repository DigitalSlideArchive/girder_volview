import { getApiRoot } from "@girder/core/rest";

export const openButton = `<a class="btn btn-sm btn-primary open-in-volview" style="margin-left: 10px" role="button">
                                <i class="icon-link-ext"></i>Open in VolView</a>`;

const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;

export function openItem(item) {
    const itemRoute = `/${getApiRoot()}/item/${item.id}`;
    const saveParam = `&save=${itemRoute}/volview`;
    const manifestUrl = `${itemRoute}/volview`;
    const downloadParams = `&names=[manifest.json]&urls=${manifestUrl}`;
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    window.open(newTabUrl, "_blank").focus();
}

function resourcesToDownloadParams(folderId, resources) {
    const items = (resources.item || []).join(",");
    const folders = (resources.folder || []).join(",");
    const manifestUrl = `/${getApiRoot()}/folder/${folderId}/volview?folders=${folders}&items=${items}`;
    return `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
}

export function openResources(folder, resources) {
    const folderRoute = `/${getApiRoot()}/folder/${folder.id}`;
    const saveParam = `&save=${folderRoute}/volview`;
    const downloadParams = resourcesToDownloadParams(folder.id, resources);
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    window.open(newTabUrl, "_blank").focus();
}
