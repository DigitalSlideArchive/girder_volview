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
    window.open(newTabUrl, "_blank").focus();
}

export const knownExtensions = [
    'aim', 'isq', 'bmp', 'jpg', 'jpeg', 'jpe', 'png', 'dcm', 'zip', 'gz',
    'hdf5', 'h5', 'pic', 'fre', 'vtk', 'gii', 'obj', 'byu', 'off', 'fsa',
    'fsb', 'fcv', 'mnc', 'nii', 'par', 'rec', 'tre', 'tfm'];
