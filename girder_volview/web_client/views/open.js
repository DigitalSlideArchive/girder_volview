import { restRequest, getApiRoot } from "@girder/core/rest";

export const openButton = `<a class="btn btn-sm btn-primary open-in-volview" style="margin-left: 10px" role="button">
                                <i class="icon-link-ext"></i>Open in VolView</a>`;

const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;

function isSessionFile(fileName) {
    return fileName.endsWith("volview.zip");
}

function makeDownloadParams(model, itemRoute, files) {
    if (files.length === 0) return "";

    const hasSessionFiles = files.some(({ name }) => isSessionFile(name));
    const { url: downloadUrl, name } = hasSessionFiles
        ? { url: `${itemRoute}/volview`, name: `${model.name()}.volview.zip` }
        : {
              url: `${itemRoute}/volview/manifest`,
              name: `${model.name()}-files.json`,
          };

    return `&names=[${name}]&urls=[${downloadUrl}]`;
}

export function openItem(item) {
    restRequest({
        type: "GET",
        url: `item/${item.id}/files?limit=0`,
        error: null,
    }).done((files) => {
        const itemRoute = `/${getApiRoot()}/item/${item.id}`;
        const saveParam = `&save=${itemRoute}/volview`;
        const downloadParams = makeDownloadParams(item, itemRoute, files);
        const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
        window.open(newTabUrl, "_blank").focus();
    });
}

function resourcesToDownloadParams(folder, resources) {
    const items = (resources.item || []).join(",");
    const folders = (resources.folder || []).join(",");
    const manifestUrl = `/${getApiRoot()}/folder/${folder}/volview_manifest?folders=${folders}&items=${items}`;
    return `&names=[manifest.json]&urls=${encodeURIComponent(manifestUrl)}`;
}

export function openResources(folder, resources) {
    const folderRoute = `/${getApiRoot()}/folder/${folder.id}`;
    const saveParam = `&save=${folderRoute}/volview`;
    const downloadParams = resourcesToDownloadParams(folder.id, resources);
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    window.open(newTabUrl, "_blank").focus();
}
