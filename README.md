# aurdex

**aurdex** is a terminal user interface (TUI) for browsing AUR package metadata.

- Filter and search packages by name, maintainer, status, and more
- View detailed metadata, dependencies, and reverse dependencies
- Explore package git repositories and commit history

> **Note**: aurdex is **not** an AUR helper. It does **not** install or build packages.  
> It’s designed for **viewing package information** and understanding dependency relationships.

![AUR_Package_Browser_2025-06-22T16_21_56_158839](https://github.com/user-attachments/assets/e05cf697-137a-4186-9bc3-04e1f2b972aa)
![AUR_Package_Browser_2025-06-22T16_22_25_364038](https://github.com/user-attachments/assets/ec024005-34b1-4ba2-9d3d-1a1834cb329c)
![AUR_Package_Browser_2025-06-22T16_22_12_416136](https://github.com/user-attachments/assets/4fef6e0e-52d6-4498-bdc8-44ca0e65a8c9)


# Installation
The aurdex tool is available in the AUR (Arch User Repository).

You can install it using an AUR helper such as yay, paru, or manually via git.

### Using an AUR helper (recommended)
```
yay -S aurdex

or

paru -S aurdex
```
### Manual installation
Clone the package and build it with makepkg:
```
git clone https://aur.archlinux.org/aurdex.git
cd aurdex
makepkg -si
```
