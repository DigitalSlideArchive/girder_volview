# Attribution

The DICOM data this devkit downloads comes from the NCI Imaging Data Commons
(IDC) and is redistributed under CC BY 3.0 / CC BY 4.0. No imaging data is
stored in this repository -- `seed.py fetch` pulls it from IDC's public bucket.

## Datasets used

- Kinahan, P., Muzi, M., Bialecki, B., Herman, B., & Coombs, L. (2019). Data from the ACRIN 6668 Trial NSCLC-FDG-PET (Version 2) [Dataset]. The Cancer Imaging Archive. https://doi.org/10.7937/TCIA.2019.30ILQFCL
- Eisenbrey, J., Lyshchik, A., & Wessner, C. (2021). Ultrasound data of a variety of liver masses (Version 1) [Dataset]. The Cancer Imaging Archive. https://doi.org/10.7937/TCIA.2021.V4Z7-TC39
- Fedorov, A., Longabaugh, W. J. R., Pot, D., Clunie, D. A., Pieper, S. D., Gibbs, D. L., Bridge, C., Herrmann, M. D., Homeyer, A., Lewis, R., Aerts, H. J. W. L., Krishnaswamy, D., Thiriveedhi, V. K., Ciausu, C., Schacherer, D. P., Bontempi, D., Pihl, T., Wagner, U., Farahani, K., et al. (2023). National Cancer Institute Imaging Data Commons: Toward Transparency, Reproducibility, and Scalability in Imaging Artificial Intelligence. RadioGraphics, 43(12). https://doi.org/10.1148/rg.230180
- Litjens, G., Debats, O., Barentsz, J., Karssemeijer, N., & Huisman, H.
  (2017). *SPIE-AAPM PROSTATEx Challenge Data* (Version 2) [Dataset]. The
  Cancer Imaging Archive. https://doi.org/10.7937/K9TCIA.2017.MURS5CL

IDC selection DOIs: 10.7937/tcia.2019.30ilqfcl, 10.7937/tcia.2021.v4z7-tc39

## VolView developer examples

The prostate subset and fetal ultrasound volume are pinned by SHA-512 and
downloaded from Kitware's public VolView example-data folder:

- `MRI-PROSTATEx-0004.zip`
- `prostate-total.seg.nii.gz` (converted locally to `.seg.nrrd`)
- `3DUS-Fetus.mha`

The fetal segmentation is generated locally with Otsu thresholding; it is not
a clinical annotation.

## Retinal OCT example

`seed.py seed-retinal` adds the image and companion segmentation from
[VolView's retinal layer sample](https://data.kitware.com/api/v1/file/6ae786c91747c43eb290172f/download).
The archive is pinned by SHA-512 in `manifest/retinal-oct.json`; its two NRRDs
are also checked against the published SHA-256 values before staging.

The sample is healthy control hc01 from the
[JHU IACL OCT dataset](https://iacl.ece.jhu.edu/index.php/OCT_Data). Please cite
He, Y., Carass, A., Solomon, S. D., Saidha, S., Calabresi, P. A., and Prince,
J. L. (2019). *Retinal layer parcellation of optical coherence tomography
images: Data resource for Multiple Sclerosis and Healthy Controls.*
Data in Brief, 22, 601-604. https://doi.org/10.1016/j.dib.2018.12.073.

[Conversion, source checksums, redistribution notes, and limitations](https://github.com/Kitware/VolView/blob/a08aabf4891167ec00cae556e1cbb2552a4e0db2/scripts/samples/jhu-oct-provenance.json)
are recorded with the published sample. The eight tissue labels are derived
from manually delineated boundaries. The converted volumes use local scan
coordinates; patient anatomical orientation is not inferred.

## IDC

Fedorov, A., Longabaugh, W. J. R., Pot, D., et al. *National Cancer Institute
Imaging Data Commons: Toward Transparency, Reproducibility, and Scalability in
Imaging Artificial Intelligence.* RadioGraphics (2023).
https://doi.org/10.1148/rg.230180

## Terms that travel with this data

For the TCIA datasets above, the TCIA Data Usage Policy
(https://www.cancerimagingarchive.net/data-usage-policies-and-restrictions/):

- Attribute each individual dataset used, and link to that policy.
- Pass this same obligation on to downstream users.
- Do not attempt to identify or contact the individuals these images came from.

Regenerate this file with `uv run seed.py fetch`.
