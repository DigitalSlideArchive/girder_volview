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

    const configUrl = `${itemRoute}/volview/config/.volview_config.yaml`;

    return `&names=[${name},config.json]&urls=[${downloadUrl},${configUrl}]`;
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

function filesToDownloadParams(files) {
    // Find session file in same folder
    // Filter out session files
    const fileUrls = files
        .map((file) => {
            return [
                "",
                getApiRoot(),
                "file",
                String(file._id),
                "proxiable",
                file.name,
            ].join("/");
        })
        .join(",");
    const fileNames = files.map((file) => file.name).join(",");

    return `&names=[${fileNames}]&urls=[${fileUrls}]`;
}

export function openFiles(folder, files) {
    const folderRoute = `/${getApiRoot()}/folder/${folder.id}`;
    const saveParam = `&save=${folderRoute}/volview`;
    const downloadParams = filesToDownloadParams(files);
    const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
    window.open(newTabUrl, "_blank").focus();
}
