Feature: Camera settings

  Scenario: Apply one camera setting
    Given the dashcam config has "VOLUME" set to "5"
    When the camera setting "Tab3.VOLUME" is applied as "4"
    Then the dashcam config has "VOLUME" set to "4"
    Then the dashcam config differs only in "VOLUME"
